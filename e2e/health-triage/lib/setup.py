#!/usr/bin/env python3
"""Sets up the health triage scenario through Caliban's admin API (control plane, admin token).

Idempotent where the API allows it: re-running reuses the tenant, model, datasource and approved
ontology, and creates a new node version only when the rendered spec changed.

Steps: the chat model (provider + catalogue entry), the tenant (PII mask, its own PII patterns,
spend caps), its routes, an application API key, the MongoDB datasource and its introspection, the
ontology (introspection's proposals and the curated CSM, reviewed in one batch), the triage node
(draft, published without promotion), its golden set and promotion bar, the golden and injection
evals, the gate, the promotion, and the `triage` intent mapped to node/triage@published for
caliban/auto. With E2E_RAG=1, then the RAG stage (lib/rag.py): the document collection, its ingest
and indexing, the `careguide` node and its API key.

The ontology goes through the public API only (core docs/datasources.md): introspection proposes
what it infers from the replica set, the curated CSM (ontology/health.csm.json: the e2e's names,
the metrics, the relation, the glossary) is proposed over it with POST
/api/v1/tenants/{t}/ontology/proposals, and one review batch approves the CSM's elements and
rejects introspection's other proposals: one ontology version.
"""
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (HERE, TENANT, HttpError, cp, info, load_state, q, save_state, step,  # noqa: E402
                    wait_healthy, wait_until)
import rag  # noqa: E402  (the optional RAG stage, E2E_RAG=1)

CHAT_ID = os.environ.get("E2E_CHAT_MODEL_ID", "e2e/chat")
NODE = "triage"


def created_or_exists(fn, what):
    try:
        r = fn()
        info(f"{what}: created")
        return r.body
    except HttpError as e:
        if e.status == 409:
            info(f"{what}: exists")
            return None
        raise


def chat_model():
    step("Chat model (shared provider and catalogue entry)")
    tier = os.environ.get("E2E_CHAT_TRUST_TIER", "t0_sovereign")
    created_or_exists(lambda: cp("POST", "/api/v1/providers", {
        "id": "e2e-llm", "kind": "openai_compatible", "base_url": os.environ.get("E2E_CHAT_BASE_URL"),
        "trust_tier": tier, "cache_salt": os.environ.get("E2E_TARGET") == "testbed"}), "provider e2e-llm")
    family = os.environ.get("E2E_MODEL_FAMILY") or None
    caps = {"tools": True}
    if family and family.startswith("qwen3"):
        caps.update({"reasoning": "hybrid", "reasoning_control": "enable_thinking"})
    created_or_exists(lambda: cp("POST", "/api/v1/models", {
        "id": CHAT_ID, "provider": "e2e-llm", "upstream_model": os.environ.get("E2E_UPSTREAM_MODEL", "e2e-scripted"),
        "kind": "chat", "family": family, "trust_tier": tier, "capabilities": caps, "context_window": 32768,
        "licence": "apache-2.0",
        "price_in_per_mtok": float(os.environ.get("E2E_PRICE_IN_PER_MTOK", "0.2")),
        "price_out_per_mtok": float(os.environ.get("E2E_PRICE_OUT_PER_MTOK", "0.8"))}), f"model {CHAT_ID}")
    info(f"{CHAT_ID} -> {os.environ.get('E2E_UPSTREAM_MODEL')} at {os.environ.get('E2E_CHAT_BASE_URL')} ({tier})")


# The tenant's own L0 PII patterns (core README, "PII"; research note 05, section 1: institutional
# identifiers that generic detectors miss). The synthetic Lebanese-style ID of R1 (LB-4471-9902-3)
# has no built-in detector. French NIR, French national phones and Lebanese international numbers
# are built in; a Lebanese number without its trunk prefix (71 234 567) would need a pattern here
# too, and no request uses one.
PII_PATTERNS = [{"label": "NATIONAL_ID", "pattern": r"\bLB-\d{4}-\d{4}-\d\b"}]


def tenant(st):
    step(f"Tenant {TENANT}")
    pii = os.environ.get("E2E_PII_MODE", "mask")
    # Off by default: in-boundary models (t0_sovereign, t1_attested) get the text as is, and the
    # node's guardrails.pii floor is what masks its calls (core docs/nodes.md). 1 also masks the
    # tenant's other calls to in-boundary models (caliban/auto's classification embedding, plain
    # model answers).
    on_sovereign = os.environ.get("E2E_PII_ON_SOVEREIGN", "0") == "1"
    caps = {"daily_usd": 20, "monthly_usd": 200}
    settings = {"pii_default": pii, "pii_on_sovereign": on_sovereign, "pii_patterns": PII_PATTERNS,
                "node_spend_caps": caps}
    t = created_or_exists(lambda: cp("POST", "/api/v1/tenants", dict(settings, name=TENANT)), f"tenant {TENANT}")
    if t is None:
        t = cp("PATCH", f"/api/v1/tenants/{TENANT}", settings).body
    st["tenant_settings"] = {k: t.get(k) for k in ("pii_default", "pii_on_sovereign", "pii_patterns")}
    info(f"pii_default={t.get('pii_default')}, pii_on_sovereign={t.get('pii_on_sovereign')}, "
         f"pii_patterns={json.dumps(t.get('pii_patterns'))}, node spend caps {caps}")
    cp("PUT", f"/api/v1/tenants/{TENANT}/routes", {"routes": [{"intent": "default", "models": [CHAT_ID]}]})
    info(f"route default -> {CHAT_ID}")
    if not st.get("api_key"):
        k = cp("POST", f"/api/v1/tenants/{TENANT}/api-keys", {"name": "medmarket-app", "nodes": [NODE]}).body
        st.update(api_key=k["key"], api_key_id=k["id"])
        info(f"API key {k['id']} (node allowlist: {NODE})")
    else:
        info(f"API key {st['api_key_id']} (from {os.path.basename(os.environ.get('E2E_WORK_DIR', '.work'))}/state.json)")


def datasource(st):
    step("Datasource (MongoDB replica set, read-only principal)")
    live = [d for d in cp("GET", f"/api/v1/datasources?tenant_id={TENANT}").body if d["name"] == "health"]
    if live:
        st["datasource_id"] = live[0]["id"]
        info(f"health: exists ({live[0]['id']}, status {live[0]['status']})")
    else:
        pw = os.environ["E2E_MONGO_READER_PASSWORD"]
        uri = (f"mongodb://caliban_reader:{pw}@caliban-e2e-mongo:27017,caliban-e2e-mongo-analytics:27017/"
               "?replicaSet=rs0&authSource=marketplace")
        d = cp("POST", "/api/v1/datasources", {"tenant_id": TENANT, "kind": "mongodb", "name": "health",
                                                "connection": {"uri": uri, "database": "marketplace"}}).body
        st["datasource_id"] = d["id"]
        info(f"health: created {d['id']}; stored connection {json.dumps(d['connection'])}")
    introspect(st)


def introspect(st):
    """Introspection (core docs/datasources.md): samples the collections through the reader and
    writes what it infers as proposed elements. A re-run skips every element already reviewed. A
    failed or timed-out job is reported; the curated CSM proposed next is enough on its own."""
    ds = st["datasource_id"]
    r = cp("POST", f"/api/v1/datasources/{ds}/introspect", ok=None)
    if r.status not in (202, 409):
        info(f"introspect -> {r.status} {json.dumps(r.body)[:300]}; going on with the curated CSM only")
        st["introspection"] = {"status": f"HTTP {r.status}"}
        return
    info(f"introspect -> {r.status} ({'started' if r.status == 202 else 'one is running already'})")
    timeout = int(os.environ.get("E2E_INTROSPECT_TIMEOUT_S", "300"))
    try:
        job = wait_until("the introspection job", lambda: (lambda j: j if j.get("status") != "running" else None)(
            cp("GET", f"/api/v1/datasources/{ds}/introspection").body), timeout=timeout, every=2)
    except TimeoutError as e:
        info(f"{e}; going on with the curated CSM only")
        st["introspection"] = {"status": "timeout"}
        return
    st["introspection"] = {k: job.get(k) for k in ("id", "status", "objects", "proposed", "skipped", "version", "error")}
    info(f"introspection {job.get('status')}: {job.get('objects')} collections, {job.get('proposed')} proposed, "
         f"{job.get('skipped')} skipped (already reviewed or unchanged), version {job.get('version')}"
         + (f", error {job['error']}" if job.get("error") else ""))


def covers(current, wanted):
    """Whether a stored value says what the file says (keys the file leaves out may be filled in)."""
    if isinstance(wanted, dict):
        return isinstance(current, dict) and all(covers(current.get(k), v) for k, v in wanted.items())
    return current == wanted


def ontology(st):
    step("Ontology (CSM): introspection, curated proposals, one review batch")
    path = f"/api/v1/tenants/{TENANT}/ontology"
    with open(os.path.join(HERE, "ontology", "health.csm.json"), encoding="utf-8") as f:
        csm = json.load(f)
    curated = {e["id"]: e for e in csm["elements"]}
    onto = cp("GET", path).body
    current = {e["id"]: e for e in onto["elements"]}
    # Propose only what is not approved as the file says: proposing over an approved element
    # withdraws it until it is approved again (and is refused while a published node queries it).
    fresh = [e for i, e in curated.items()
             if not (current.get(i, {}).get("status") == "approved"
                     and current[i].get("kind") == e["kind"] and covers(current[i].get("spec"), e["spec"]))]
    if fresh:
        p = cp("POST", f"{path}/proposals", {"elements": fresh}).body
        info(f"curated CSM: {p.get('proposed')} proposed, {p.get('skipped')} unchanged, version {p.get('version')}")
    else:
        info(f"curated CSM: all {len(curated)} elements approved already")
    onto = cp("GET", path).body
    before = onto["version"]
    pending = [e for e in onto["elements"] if e["status"] == "proposed"]
    decisions = [{"id": e["id"], "decision": "approve" if e["id"] in curated else "reject"} for e in pending]
    if decisions:
        r = cp("POST", f"{path}/review", {"decisions": decisions,
                                         "note": "e2e: reviewed against the synthetic marketplace schema"}).body
        n_ok = sum(1 for d in decisions if d["decision"] == "approve")
        info(f"review batch: {n_ok} approved, {len(decisions) - n_ok} rejected (introspection's names the CSM "
             f"does not use), version {before} -> {r['version']}")
        st["review_batch"] = {"decisions": len(decisions), "from": before, "to": r["version"]}
    onto = cp("GET", path).body
    approved = [e for e in onto["elements"] if e["status"] == "approved"]
    missing = sorted(set(curated) - {e["id"] for e in approved})
    if missing:
        raise RuntimeError(f"curated elements not approved: {missing}")
    st["ontology_version"] = f"{TENANT}@{onto['version']}"
    by_kind = {}
    for e in approved:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    info(f"{len(approved)} approved {by_kind}; version {st['ontology_version']}")


def render_spec(st):
    with open(os.path.join(HERE, "node", "triage.node.json"), encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__ONTOLOGY_VERSION__", st["ontology_version"]).replace("__CHAT_MODEL__", CHAT_ID)
    # Reasoning effort per model vertex (core docs/nodes.md, "Reasoning and constrained decoding";
    # research note 02, "When to Reason"): classifying the case and picking from returned rows need
    # no thinking, and a thinking Qwen3.8 call takes tens of seconds. The gateway maps it per family
    # (Qwen3: chat_template_kwargs.enable_thinking).
    for vertex in ("assess", "recommend"):
        effort = os.environ.get(f"E2E_{vertex.upper()}_REASONING", "off")
        if effort not in ("off", "low", "medium", "high"):
            sys.exit(f"E2E_{vertex.upper()}_REASONING must be off, low, medium or high (got {effort!r})")
        text = text.replace(f"__{vertex.upper()}_REASONING__", effort)
    spec = json.loads(text)
    # The recommend vertex answers with the node's output schema (one corrective retry on a miss);
    # copied here so the two never drift apart.
    for v in spec["graph"]["vertices"]:
        if v["id"] == "recommend":
            v["config"]["output_schema"] = copy.deepcopy(spec["prompt"]["output_schema"])
    return spec


def node(st):
    step("Node triage: draft, publish (not promoted yet)")
    spec = render_spec(st)
    versions = cp("GET", f"/api/v1/tenants/{TENANT}/nodes/{NODE}/versions", ok=None)
    existing = versions.body.get("versions", []) if versions.status == 200 else []
    same = [v for v in existing if v["spec"] == spec and v["state"] != "retired"]
    if same:
        v = same[-1]
        info(f"v{v['version']} has this spec already ({v['state']}, {v['hash'][:19]}...)")
    else:
        v = cp("POST", f"/api/v1/tenants/{TENANT}/nodes/{NODE}/versions", {"spec": spec}).body
        info(f"created draft v{v['version']} {v['hash'][:19]}...")
    if v["state"] == "draft":
        v = cp("POST", f"/api/v1/tenants/{TENANT}/nodes/{NODE}/versions/{v['version']}/publish", {"promote": False}).body
        info(f"published v{v['version']} (live: {v.get('live')})")
    for w in v.get("warnings") or []:
        info(f"warning: {w}")
    st["node_version"] = v["version"]
    st["node_hash"] = v["hash"]


def evals(st):
    ver = st["node_version"]
    base = f"/api/v1/tenants/{TENANT}/nodes/{NODE}"
    step(f"Golden set, promotion bar, evals of v{ver}")
    with open(os.path.join(HERE, "node", "golden.json"), encoding="utf-8") as f:
        golden = json.load(f)
    cp("PUT", f"{base}/versions/{ver}/golden-set", golden)
    info(f"golden set: {len(golden['cases'])} cases (en, fr, ar; two with a scripted human answer)")
    bar = {"golden": {"min_pass_k": float(os.environ.get("E2E_MIN_PASS_K", "0.8")), "min_k": 3,
                      "max_cost_per_success_usd": 0.05,
                      "max_p95_latency_ms": int(os.environ.get("E2E_MAX_P95_MS", "60000"))},
           "injection": {"min_pass_rate": 1.0}}
    cp("PUT", f"{base}/eval-policy", bar)
    info(f"promotion bar: {json.dumps(bar)}")
    started = {}
    for suite, body in (("golden", {"suite": "golden", "k": 3, "concurrency": 4, "judge_model": CHAT_ID}),
                        ("injection", {"suite": "injection", "input_case": "en-chest-beirut"})):
        e = cp("POST", f"{base}/versions/{ver}/evals", body).body
        started[suite] = e["id"]
        info(f"{suite} eval {e['id']} started ({e['case_count']} cases, k={e['k']})")
    timeout = int(os.environ.get("E2E_EVAL_TIMEOUT_S", "1800"))
    results = {}
    for suite, eid in started.items():
        e = wait_until(f"the {suite} eval", lambda: (lambda b: b if b["status"] in ("succeeded", "failed", "cancelled") else None)(
            cp("GET", f"/api/v1/tenants/{TENANT}/evals/{eid}").body), timeout=timeout, every=2)
        results[suite] = e
        info(f"{suite}: {e['status']} {json.dumps(e.get('summary'), ensure_ascii=False)[:1500]}")
        if e.get("error"):
            info(f"{suite} error: {e['error']}")
    st["evals"] = {s: {"id": e["id"], "status": e["status"], "summary": e.get("summary"),
                       "trials": [{k: t.get(k) for k in ("case", "trial", "passed", "run_id", "verdict")}
                                  for t in e.get("trials", [])]} for s, e in results.items()}


def gate_and_promote(st):
    ver = st["node_version"]
    base = f"/api/v1/tenants/{TENANT}/nodes/{NODE}"
    step(f"Gate and promotion of v{ver}")
    g = cp("GET", f"{base}/versions/{ver}/gate").body
    st["gate"] = g
    info(f"gate: {json.dumps(g, ensure_ascii=False)[:1500]}")
    try:
        p = cp("POST", f"{base}/promote", {"version": ver}).body
        info(f"promoted: live v{p.get('version', ver)}")
        st["promoted"] = True
    except HttpError as e:
        st["promoted"] = False
        info(f"promotion refused: {e}")
        if os.environ.get("E2E_FORCE_PROMOTE") != "1":
            raise


def intent(st):
    """The tenant's caliban/auto intents through the admin API (core docs/nodes.md, "caliban/auto
    and nodes"): the exemplars, then the mapping to the node. Routers rebuild their kNN index with
    the snapshot that carries them, without a restart."""
    step("caliban/auto: intent triage (exemplars) -> node/triage@published")
    with open(os.path.join(HERE, "config", "exemplars.json"), encoding="utf-8") as f:
        intents = json.load(f)["intents"]
    for name, exemplars in intents.items():
        r = cp("PUT", f"/api/v1/tenants/{TENANT}/intents/{q(name)}", {"exemplars": exemplars}).body
        info(f"intent {name}: {len(r.get('exemplars') or [])} exemplars (source {r.get('source')})")
    t = cp("PATCH", f"/api/v1/tenants/{TENANT}", {"node_routes": {"triage": f"node/{NODE}@published"}}).body
    info(f"node_routes: {t.get('node_routes')}")
    listed = cp("GET", f"/api/v1/tenants/{TENANT}/intents").body
    st["intents"] = {i["intent"]: {"exemplars": len(i.get("exemplars") or []), "source": i.get("source"),
                                   "node": i.get("node")} for i in listed.get("intents") or []}
    info(f"intents as routers see them: {json.dumps(st['intents'])}")
    # Routers apply the change with their next snapshot (embedding the exemplars on the way).
    time.sleep(int(os.environ.get("E2E_INTENT_SETTLE_S", "5")))


def main():
    if not os.environ.get("CALIBAN_ADMIN_TOKEN"):
        sys.exit("CALIBAN_ADMIN_TOKEN is not set (run through e2e.sh)")
    st = load_state()
    wait_healthy()
    h = cp("GET", "/api/v1/health").body
    info(f"control plane: {json.dumps(h)[:300]}")
    chat_model()
    tenant(st)
    save_state(st)
    datasource(st)
    ontology(st)
    save_state(st)
    node(st)
    save_state(st)
    evals(st)
    save_state(st)
    gate_and_promote(st)
    intent(st)
    save_state(st)
    if rag.ENABLED:
        rag.setup(st)
        save_state(st)
    step("Setup done")
    info(f"tenant {TENANT}, node triage v{st['node_version']}, ontology {st['ontology_version']}")
    if rag.ENABLED:
        info(f"retrieval: collection {st['rag']['collection']}, node {rag.NODE} v{st['rag']['node_version']}")


if __name__ == "__main__":
    main()
