#!/usr/bin/env python3
"""Seeds the law firm's practice database before the recording (stdlib only, Python 3.9+).

    seed.py            seed MongoDB database `lawfirm` in the shared e2e replica set
    seed.py --print    print the mongosh script instead of running it

Writes `clients`, `matters` and `time_entries` (lib/dataset.py: synthetic, dates relative to now),
creates the read-only principal Caliban connects as (`caliban_law_reader`, role `read` on
`lawfirm` only, password E2E_LAW_READER_PASSWORD, else the e2e's E2E_MONGO_READER_PASSWORD), and
turns profiling on for `lawfirm` on both members, so check.py can show the pipelines each node run
sent and which member answered them. Re-running drops and recreates the database.

Runs mongosh inside the primary's container through the shared stack's compose command
(E2E_COMPOSE_CMD, root credentials E2E_MONGO_ROOT_USER and E2E_MONGO_ROOT_PASSWORD), like
e2e/health-triage/e2e.sh up. For a test without Docker, E2E_LAW_MONGO_ROOT_URI and pymongo do the
same through a driver.
"""
import json
import os
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import dataset  # noqa: E402
import law  # noqa: E402

DATES = {"clients": ["client_since"], "matters": ["opened_at", "deadlines.due_at"], "time_entries": ["worked_on"]}

SCRIPT = r"""
const DATA = %(data)s;
const DATES = %(dates)s;
const toDate = (doc, path) => {
  const [head, ...rest] = path.split(".");
  if (rest.length === 0) { if (doc[head]) doc[head] = new Date(doc[head]); return; }
  for (const el of doc[head] || []) toDate(el, rest.join("."));
};
const d = db.getSiblingDB("%(db)s");
d.dropDatabase();
for (const [coll, docs] of Object.entries(DATA)) {
  for (const doc of docs) for (const p of DATES[coll] || []) toDate(doc, p);
  d.getCollection(coll).insertMany(docs, { writeConcern: { w: "majority" } });
}
d.time_entries.createIndex({ matter_id: 1 });
d.matters.createIndex({ client_id: 1 });
const reader = "%(reader)s";
if (d.getUsers({ filter: { user: reader } }).users.length === 0) {
  d.createUser({ user: reader, pwd: process.env.E2E_LAW_READER_PASSWORD, roles: [{ role: "read", db: "%(db)s" }] });
} else {
  d.updateUser(reader, { pwd: process.env.E2E_LAW_READER_PASSWORD, roles: [{ role: "read", db: "%(db)s" }] });
}
for (let i = 0; i < 120; i++) {
  const st = rs.status().members;
  const p = st.find((x) => x.stateStr === "PRIMARY");
  const a = st.find((x) => x.name.startsWith("caliban-e2e-mongo-analytics"));
  if (!a || (a.stateStr === "SECONDARY" && a.optimeDate >= p.optimeDate)) break;
  sleep(500);
}
d.setProfilingLevel(2);
print(JSON.stringify(Object.fromEntries(Object.keys(DATA).map((c) => [c, d.getCollection(c).countDocuments()]))));
"""


def script():
    docs = dataset.documents()
    return SCRIPT % {"data": json.dumps(docs, ensure_ascii=False), "dates": json.dumps(DATES), "db": law.DATABASE,
                     "reader": law.READER}, docs


def with_pymongo(uri, docs):
    from datetime import datetime

    import pymongo  # only for local tests without Docker

    def conv(doc, p):
        head, _, rest = p.partition(".")
        if not rest:
            if doc.get(head):
                doc[head] = datetime.fromisoformat(doc[head].replace("Z", "+00:00"))
            return
        for el in doc.get(head) or []:
            conv(el, rest)

    c = pymongo.MongoClient(uri)
    d = c[law.DATABASE]
    c.drop_database(law.DATABASE)
    for coll, items in docs.items():
        for doc in items:
            for p in DATES[coll]:
                conv(doc, p)
        d[coll].insert_many(items)
    d.time_entries.create_index("matter_id")
    d.matters.create_index("client_id")
    pw = law.reader_password()
    if d.command("usersInfo", law.READER)["users"]:
        d.command("updateUser", law.READER, pwd=pw, roles=[{"role": "read", "db": law.DATABASE}])
    else:
        d.command("createUser", law.READER, pwd=pw, roles=[{"role": "read", "db": law.DATABASE}])
    return {k: d[k].count_documents({}) for k in docs}


def main():
    js, docs = script()
    if "--print" in sys.argv:
        print(js)
        return
    if not law.reader_password():
        sys.exit("seed.py: set E2E_LAW_READER_PASSWORD or E2E_MONGO_READER_PASSWORD (run through the e2e environment)")
    root_uri = os.environ.get("E2E_LAW_MONGO_ROOT_URI")
    if root_uri:
        counts = with_pymongo(root_uri, docs)
        print(f"seed.py: {law.DATABASE} seeded through the driver: {json.dumps(counts)}")
        return
    cmd = os.environ.get("E2E_COMPOSE_CMD")
    if not cmd:
        sys.exit("seed.py: E2E_COMPOSE_CMD is not set (run through e2e/health-triage/e2e.sh exec, or set "
                 "E2E_LAW_MONGO_ROOT_URI for a driver connection)")
    user, pw = os.environ["E2E_MONGO_ROOT_USER"], os.environ["E2E_MONGO_ROOT_PASSWORD"]
    env = dict(os.environ, E2E_LAW_READER_PASSWORD=law.reader_password())
    root = f"mongodb://{user}:{pw}@localhost:27017/?authSource=admin"
    # The script goes in on stdin as a file (it is too long for an argument).
    r = subprocess.run(shlex.split(cmd) + ["exec", "-T", "-e", "E2E_LAW_READER_PASSWORD", "caliban-e2e-mongo", "sh", "-c",
                                           f"cat > /tmp/law-seed.js && mongosh --quiet '{root}&replicaSet=rs0' "
                                           "--file /tmp/law-seed.js; rc=$?; rm -f /tmp/law-seed.js; exit $rc"],
                       input=js, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.exit(f"seed.py: mongosh failed: {r.stderr.strip()[-1500:]}")
    print(f"seed.py: {law.DATABASE} seeded: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''}")
    # Profiling on the analytics member too (where the native lane reads).
    r = subprocess.run(shlex.split(cmd) + [
        "exec", "-T", "caliban-e2e-mongo-analytics", "mongosh", "--quiet",
        f"mongodb://{user}:{pw}@localhost:27017/{law.DATABASE}?authSource=admin&directConnection=true",
        "--eval", "db.setProfilingLevel(2); print('profiling on')"], capture_output=True, text=True)
    print(f"seed.py: analytics member: {(r.stdout or r.stderr).strip()[-300:]}")
    print(f"seed.py: read-only principal {law.READER} (role read on {law.DATABASE})")


if __name__ == "__main__":
    main()
