#!/usr/bin/env python3
"""Seeds Cedar Bank's MongoDB database (`neobank`) in the shared e2e replica set, before recording.

Runs on the gateway host with the shared stack's environment (e2e/demo/README.md, "The runner"):
E2E_COMPOSE_CMD, E2E_MONGO_ROOT_USER, E2E_MONGO_ROOT_PASSWORD, E2E_WORK_DIR. Idempotent: the
collections are dropped and written again, the reader is created or its password reset.

1. Secrets, generated once into $E2E_WORK_DIR/fintech/ (mode 0600, never printed): the password
   of the read-only principal `caliban_neobank_reader` and the key Caliban sends to the bank's
   refund server (bank-ops.key, read by mcp/bank_ops.py on every request).
2. Database `neobank` on the primary: 5 customers, their 5 current accounts (an IBAN and a debit
   card each) and 21 transactions in September and October 2026, all synthetic (lib/fintech.py).
3. The principal `caliban_neobank_reader`: role `read` on `neobank` only (Caliban refuses a
   principal that can write).
4. The profiler (level 2) on the analytics secondary for `neobank`: check.py reads the pipelines
   Caliban's native lane ran there, with their `caliban:node:<run>:<step>` comments.
"""
import json
import os
import secrets
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import fintech  # noqa: E402


def compose(*args, env=None, input_text=None):
    cmd = shlex.split(os.environ["E2E_COMPOSE_CMD"]) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, input=input_text, env=dict(os.environ, **(env or {})))
    if r.returncode != 0:
        raise SystemExit(f"seed: {' '.join(args[:3])} failed: {r.stderr.strip()[-1500:]}")
    return r.stdout


def secret(path, nbytes):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(nbytes))
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


SEED_JS = r"""
const data = JSON.parse(process.env.FINTECH_SEED);
const nb = db.getSiblingDB('neobank');
for (const c of ['customers', 'accounts', 'transactions']) { nb.getCollection(c).drop(); }
nb.customers.insertMany(data.customers.map(c => ({
  _id: c.customer_id, full_name: c.name, email: c.email, phone: c.phone, segment: c.segment,
  kyc_status: 'verified', joined_at: new Date(c.joined_at) })));
nb.accounts.insertMany(data.accounts.map(a => ({
  _id: a.account_id, customer_id: a.customer_id, holder_name: a.holder_name, iban: a.iban, currency: a.currency,
  balance: a.balance, status: a.status, product: a.product, card: a.card })));
nb.transactions.insertMany(data.transactions.map(t => ({
  _id: t.tx_id, customer_id: t.customer_id, account_id: t.account_id, booked_at: new Date(t.booked_at),
  merchant: t.merchant, category: t.category, amount: t.amount, currency: t.currency, channel: t.channel,
  status: t.status, reference: t.reference, card_last4: t.card_last4 })));
nb.accounts.createIndex({ customer_id: 1 });
nb.transactions.createIndex({ customer_id: 1, booked_at: -1 });
nb.transactions.createIndex({ account_id: 1 });
const pwd = process.env.FINTECH_READER_PASSWORD;
const role = [{ role: 'read', db: 'neobank' }];
if (nb.getUser('caliban_neobank_reader')) {
  nb.updateUser('caliban_neobank_reader', { pwd: pwd, roles: role });
} else {
  nb.createUser({ user: 'caliban_neobank_reader', pwd: pwd, roles: role });
}
// Wait until the analytics secondary has the writes, so the first query finds the rows.
db.getSiblingDB('neobank').runCommand({ insert: '_seed_marker', documents: [{ at: new Date() }],
  writeConcern: { w: 'majority', wtimeout: 30000 } });
nb.getCollection('_seed_marker').drop();
print(JSON.stringify({ customers: nb.customers.countDocuments(), accounts: nb.accounts.countDocuments(),
  transactions: nb.transactions.countDocuments() }));
"""


def main():
    for k in ("E2E_COMPOSE_CMD", "E2E_MONGO_ROOT_USER", "E2E_MONGO_ROOT_PASSWORD"):
        if not os.environ.get(k):
            raise SystemExit(f"seed: {k} is not set (run through e2e/demo/run-demo.sh or e2e.sh exec)")
    reader_pw = secret(fintech.READER_SECRET, 24)
    secret(fintech.MCP_KEY_FILE, 32)
    os.chmod(fintech.FINTECH_WORK, 0o700)
    # The refund server's ledger starts empty with every seed: check.py reads what one recording sent.
    if os.path.exists(fintech.LEDGER):
        os.remove(fintech.LEDGER)
    segments = {"CUS-1001": "standard", "CUS-1002": "premium", "CUS-1003": "standard", "CUS-1004": "premium",
                "CUS-1005": "standard"}
    data = {
        "customers": [dict(c, segment=segments[c["customer_id"]], joined_at="2024-03-1%dT10:00:00Z" % i)
                      for i, c in enumerate(fintech.customers())],
        "accounts": fintech.accounts(),
        "transactions": fintech.transactions(),
    }
    user, pw = os.environ["E2E_MONGO_ROOT_USER"], os.environ["E2E_MONGO_ROOT_PASSWORD"]
    root = f"mongodb://{user}:{pw}@localhost:27017/?authSource=admin&replicaSet=rs0"
    out = compose("exec", "-T", "-e", "FINTECH_SEED", "-e", "FINTECH_READER_PASSWORD", "caliban-e2e-mongo",
                  "mongosh", "--quiet", root, "--eval", SEED_JS,
                  env={"FINTECH_SEED": json.dumps(data), "FINTECH_READER_PASSWORD": reader_pw})
    print(f"seed: neobank {out.strip().splitlines()[-1] if out.strip() else ''}")
    direct = f"mongodb://{user}:{pw}@localhost:27017/neobank?authSource=admin&directConnection=true"
    compose("exec", "-T", "caliban-e2e-mongo-analytics", "mongosh", "--quiet", direct, "--eval",
            "db.setProfilingLevel(2).ok")
    print("seed: profiler on the analytics secondary (neobank, level 2); reader caliban_neobank_reader (read on "
          "neobank); secrets in " + fintech.FINTECH_WORK)


if __name__ == "__main__":
    main()
