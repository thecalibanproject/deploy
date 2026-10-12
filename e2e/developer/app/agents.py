"""Step 3: publish the support agents, evaluate them, pass the gate, promote, and route to them.

    python agents.py docs       # the product docs collection for docs-helper (ingest, wait until indexed)
    python agents.py publish    # every node: a new version when its spec changed, published, not live yet
    python agents.py gate       # what promoting the orchestrator would say now
    python agents.py evals      # golden sets, promotion bars, golden and injection evals (waits)
    python agents.py promote    # the gate of each node, children first, then make it live
    python agents.py route      # caliban/auto: support intents -> node/support-orchestrator
    python agents.py all

Specs and golden sets are in support_agents.py. Node versions are kept in $DEV_STATE.
"""
import os
import sys
import time

from common import CHAT_MODEL, HERE, TENANT, admin, data, refresh_routing, load_state, ms, raw, save_state, table
import support_agents as sa
from caliban import ConflictError, NotFoundError

CHILDREN = ["billing-agent", "bug-triage", "docs-helper"]
ORDER = CHILDREN + ["support-dispatch", "support-orchestrator"]
K = int(os.environ.get("DEV_EVAL_K", "3"))
MIN_PASS_K = float(os.environ.get("DEV_MIN_PASS_K", "0.66"))
MAX_P95_MS = int(os.environ.get("DEV_MAX_P95_MS", "120000"))
EVAL_TIMEOUT_S = float(os.environ.get("DEV_EVAL_TIMEOUT_S", "2400"))
EMBED_MODEL = os.environ.get("DEV_EMBED_MODEL", "dev/embed")
RERANK_MODEL = os.environ.get("DEV_RERANK_MODEL", "")
SUPPORT_INTENTS = ["billing", "bug_report", "how_to", "multi_issue"]


def node_path(name):
    return f"tenants/{TENANT}/nodes/{name}"


def spec_of(name, st):
    v = st.get("versions", {})
    if name == "billing-agent":
        return sa.billing_agent(CHAT_MODEL)
    if name == "bug-triage":
        return sa.bug_triage(CHAT_MODEL)
    if name == "docs-helper":
        return sa.docs_helper(CHAT_MODEL)
    if name == "support-dispatch":
        return sa.support_dispatch(CHAT_MODEL, v)
    return sa.support_orchestrator(CHAT_MODEL, v["support-dispatch"])


# ─────────────────────────── docs ───────────────────────────


def docs(adm, st):
    docs_dir = os.path.join(HERE, "docs")
    try:
        adm.rag.collections.create(TENANT, name=sa.COLLECTION, embedding_model=EMBED_MODEL,
                                   rerank_model=RERANK_MODEL or None,
                                   description="Acme Boards product documentation (fictional)")
        print(f"collection {sa.COLLECTION}: created (embeddings {EMBED_MODEL}"
              + (f", rerank {RERANK_MODEL})" if RERANK_MODEL else ")"))
    except ConflictError:
        print(f"collection {sa.COLLECTION}: exists")
    names = sorted(f for f in os.listdir(docs_dir) if f.endswith(".md"))
    for f in names:
        with open(os.path.join(docs_dir, f), encoding="utf-8") as fh:
            content = fh.read()
        d = adm.rag.documents.ingest(TENANT, sa.COLLECTION, doc_id=f[:-3], content=content,
                                     title=content.splitlines()[0].lstrip("# "), content_type="text/markdown",
                                     source="docs-site", trust="trusted")
        print(f"ingest {f[:-3]:<24} v{d.version} {'unchanged' if d.unchanged else d.status}")
    deadline = time.monotonic() + float(os.environ.get("DEV_INDEX_TIMEOUT_S", "600"))
    while True:
        listed = adm.rag.documents.list(TENANT, sa.COLLECTION)
        items = getattr(listed, "documents", None) or getattr(listed, "data", None) or listed
        done = [d for d in items if d.status in ("ready", "quarantined", "failed")]
        if len(done) >= len(names) or time.monotonic() > deadline:
            break
        time.sleep(2)
    print()
    print(table([(d.doc_id, d.status, d.chunks, d.tokens) for d in items], ["document", "status", "chunks", "tokens"]))


# ─────────────────────────── publish ───────────────────────────


def publish_one(adm, st, name):
    spec = spec_of(name, st)
    try:
        listed = raw(adm, "GET", f"{node_path(name)}/versions")
    except NotFoundError:  # a node without versions yet
        listed = {"versions": []}
    same = [v for v in listed.get("versions") or [] if v["spec"] == spec and v["state"] != "retired"]
    if same:
        v = same[-1]
        what = "unchanged"
    else:
        v = adm.nodes.create(tenant_id=TENANT, name=name, spec=spec).model_dump()
        what = "created"
        v = raw(adm, "GET", f"{node_path(name)}/versions/{v['version']}")
    if v["state"] == "draft":
        v = raw(adm, "POST", f"{node_path(name)}/versions/{v['version']}/publish", {"promote": False})
        what += ", published"
    st.setdefault("versions", {})[name] = v["version"]
    save_state(st)
    return (name, f"v{v['version']}", v["state"], v["hash"][:19] + "...", what)


def publish(adm, st):
    rows = [publish_one(adm, st, n) for n in ORDER]
    print(table(rows, ["node", "version", "state", "hash", ""]))
    v = st["versions"]
    print(f"\nsupport-orchestrator v{v['support-orchestrator']} pins node://support-dispatch@v{v['support-dispatch']}, "
          f"which pins billing-agent@v{v['billing-agent']}, bug-triage@v{v['bug-triage']}, docs-helper@v{v['docs-helper']}")


# ─────────────────────────── evals ───────────────────────────


def wait_eval(adm, eid):
    """adm.evals.wait, polling the raw JSON: the SDK's Eval model rejects an eval whose trials are
    still in flight (their verdict is not one of the SDK's verdict shapes yet), so only the ended
    eval is parsed."""
    deadline = time.monotonic() + EVAL_TIMEOUT_S
    while True:
        body = raw(adm, "GET", f"/api/v1/tenants/{TENANT}/evals/{eid}")
        if body.get("status") in ("succeeded", "failed", "cancelled"):
            return adm.evals.get(TENANT, eid)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"eval {eid} still {body.get('status')} after {EVAL_TIMEOUT_S:g}s")
        time.sleep(3)


def evals(adm, st):
    v = st["versions"]
    started = []
    for name in ORDER:
        ver = v[name]
        if name in sa.GOLDEN:
            adm.evals.put_golden_set(TENANT, name, ver, sa.GOLDEN[name])
            adm.evals.set_policy(TENANT, name, golden={"min_pass_k": MIN_PASS_K, "min_k": K,
                                                       "max_cost_per_success_usd": 0.25,
                                                       "max_p95_latency_ms": MAX_P95_MS},
                                 injection={"min_pass_rate": 1.0})
            e = adm.evals.start(TENANT, name, ver, suite="golden", k=K, concurrency=4, judge_model=CHAT_MODEL)
            started.append((name, ver, "golden", e.id))
        gate = adm.evals.gate(TENANT, name, ver)
        if gate.has_untrusted_reads or gate.has_write_tools:
            kw = {"input_case": sa.GOLDEN[name][0]["id"]} if name in sa.GOLDEN else {
                "input": {"agent": "docs", "request": "How do I export a report to CSV?"}}
            e = adm.evals.start(TENANT, name, ver, suite="injection", **kw)
            started.append((name, ver, "injection", e.id))
    print(f"started {len(started)} evals: " + ", ".join(f"{n} {s}" for n, _, s, _ in started), flush=True)
    rows, out = [], {}
    for name, ver, suite, eid in started:
        e = wait_eval(adm, eid)
        s = e.summary if isinstance(e.summary, dict) else (e.summary.model_dump() if e.summary else {})
        out.setdefault(name, {})[suite] = {"id": e.id, "status": e.status, "summary": s}
        if suite == "golden":
            lat = (s.get("latency_ms") or {})
            cps = s.get("cost_per_success_usd")
            rows.append((name, f"v{ver}", suite, e.status, f"pass^{e.k} {s.get('pass_hat_k')}", f"pass@1 {s.get('pass_at_1')}",
                         "-" if cps is None else f"${cps:.4f}/success", f"p95 {ms(lat.get('p95'))} ms"))
        else:
            na = len(s.get("not_applicable") or [])
            rows.append((name, f"v{ver}", suite, e.status, f"blocked {s.get('pass_rate')}",
                         f"attacked {s.get('attacked', '-')}", f"{na} n/a", ""))
    st["evals"] = out
    save_state(st)
    print(table(rows, ["node", "version", "suite", "status", "", "", "cost", "latency"]))


# ─────────────────────────── promote ───────────────────────────


def _d(x):
    return x if isinstance(x, dict) or x is None else x.model_dump()


def show_gate(adm, name, ver):
    """What promoting would say now (GET .../versions/{v}/gate), children's evidence included."""
    g = adm.evals.gate(TENANT, name, ver)
    ev = _d(g.evidence) or {}
    print(f"{name} v{ver}: {'meets the bar' if g.allowed else 'BLOCKED by the promotion bar'}")
    for p in g.problems or []:
        p = _d(p)
        print(f"   x {p.get('message')}" + (f"  [{p['path']}]" if p.get("path") else ""))

    def line(label, e, pad):
        e = e or {}
        bits = []
        if e.get("golden"):
            bits.append(f"golden pass^{e['golden'].get('k')} {e['golden'].get('pass_hat_k')}")
        if e.get("injection"):
            bits.append(f"injection blocked {e['injection'].get('pass_rate')}")
        print(f"{pad}ok {label}: " + (", ".join(bits) or "no bar of its own (no tools, no golden bar)"))

    if g.allowed:
        line(f"{name}@v{ver}", ev, "   ")
        for child, e in sorted((ev.get("subnodes") or {}).items()):
            line(child, e, "      ")
    return g


def gate(adm, st, name="support-orchestrator"):
    show_gate(adm, name, st["versions"][name])


def promote(adm, st):
    v = st["versions"]
    live = {}
    for name in ORDER:
        g = show_gate(adm, name, v[name])
        if g.allowed:
            raw(adm, "POST", f"{node_path(name)}/promote", {"version": v[name]})
            print(f"   -> {name} v{v[name]} is live")
        live[name] = bool(g.allowed)
    st["promoted"] = live
    save_state(st)
    if not all(live.values()):
        sys.exit(1)


# ─────────────────────────── route ───────────────────────────


def route(adm, st):
    # A message with several problems is its own class: its votes would split between billing and
    # bug_report otherwise, and kNN abstains on a split vote.
    for intent, exemplars in data("intents.multi.json")["intents"].items():
        r = adm.intents.set(TENANT, intent, exemplars)
        print(f"PUT intents/{intent} {len(r.exemplars)} exemplars")
    routes = {i: "node/support-orchestrator@published" for i in SUPPORT_INTENTS}
    t = raw(adm, "PATCH", f"tenants/{TENANT}", {"node_routes": routes})
    by_target = {}
    for intent, target in (t.get("node_routes") or {}).items():
        by_target.setdefault(target, []).append(intent)
    for target, intents in by_target.items():
        print(f"PATCH tenant node_routes: {', '.join(sorted(intents))} -> {target}")
    refresh_routing()
    print()
    rows = [(i.intent, len(i.exemplars or []), i.node or "a model answers")
            for i in adm.intents.list(TENANT).intents if i.source == "api"]
    print(table(rows, ["intent", "exemplars", "hands to"]))


def main(cmd):
    adm = admin()
    st = load_state()
    steps = {"docs": docs, "publish": publish, "gate": gate, "evals": evals, "promote": promote, "route": route}
    for name in (["docs", "publish", "evals", "promote", "route"] if cmd == "all" else [cmd]):
        if cmd == "all":
            print(f"\n== {name}")
        steps[name](adm, st)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
