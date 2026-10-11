#!/usr/bin/env python3
"""Sets up the health triage scenario through Caliban's admin API (control plane, admin token).

Idempotent where the API allows it: re-running reuses the tenant, model, datasource and approved
ontology, and creates a new node version only when the rendered spec changed.

Steps: the chat model (provider + catalogue entry), the tenant (PII mask, spend caps), its routes,
an application API key, the MongoDB datasource, its ontology (proposed, then approved one element at
a time), the triage node (draft, published without promotion), its golden set and promotion bar,
the golden and injection evals, the gate, the promotion, and the `triage` intent mapped to
node/triage@published for caliban/auto.

One step cannot use the public API: core has no endpoint that writes proposed ontology elements
(POST /api/v1/datasources/{id}/introspect only marks the datasource `introspecting`, see
crates/caliban-cp/src/lib.rs introspect_datasource). The script writes the proposals the way the
store does (one ontology commit, crates/caliban-cp/src/store/postgres.rs ontology_commit) with psql,
restarts Caliban so the store reloads, and approves every element through the public review API.
"""
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (HERE, TENANT, HttpError, compose, cp, info, load_state, q, save_state,  # noqa: E402
                    step, wait_healthy, wait_until)

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
    tier = os.environ.get("E2E_CHAT_TRUST_TIER", "t2_contracted")
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


def tenant(st):
    step(f"Tenant {TENANT}")
    pii = os.environ.get("E2E_PII_MODE", "mask")
    caps = {"daily_usd": 20, "monthly_usd": 200}
    t = created_or_exists(lambda: cp("POST", "/api/v1/tenants", {
        "name": TENANT, "pii_default": pii, "node_spend_caps": caps}), f"tenant {TENANT}")
    if t is None:
        cp("PATCH", f"/api/v1/tenants/{TENANT}", {"pii_default": pii, "node_spend_caps": caps})
    info(f"pii_default={pii}, node spend caps {caps}")
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
    r = cp("POST", f"/api/v1/datasources/{st['datasource_id']}/introspect", ok=None)
    info(f"introspect -> {r.status} {json.dumps(r.body)} (core: no job runs yet; proposals come next)")


def sql_literal(s):
    return "'" + s.replace("'", "''") + "'"


def ontology(st):
    step("Ontology (CSM): proposals, review, approved version")
    onto = cp("GET", f"/api/v1/ontology?tenant_id={TENANT}").body
    with open(os.path.join(HERE, "ontology", "health.csm.json"), encoding="utf-8") as f:
        csm = json.load(f)
    if not onto["elements"]:
        info(f"writing {len(csm['elements'])} proposed elements as one ontology commit (psql; no public API)")
        stmts = ["BEGIN;",
                 "INSERT INTO ontology_commit (tenant_id, parent_id, author, message) VALUES "
                 f"({sql_literal(TENANT)}, NULL, 'e2e-introspection', 'ontology.propose (e2e health triage)');"]
        for e in csm["elements"]:
            stmts.append(
                "INSERT INTO ontology_element (tenant_id, id, commit_id, kind, name, status, provenance, confidence, body) "
                f"VALUES ({sql_literal(TENANT)}, {sql_literal(e['id'])}, currval('ontology_commit_id_seq'), "
                f"{sql_literal(e['kind'])}, {sql_literal(e['name'])}, 'proposed', {sql_literal(e['provenance'])}, "
                f"{e['confidence']}, {sql_literal(json.dumps(e, ensure_ascii=False))}::jsonb);")
        stmts.append("INSERT INTO ontology_head (tenant_id, commit_id) VALUES "
                     f"({sql_literal(TENANT)}, currval('ontology_commit_id_seq')) "
                     "ON CONFLICT (tenant_id) DO UPDATE SET commit_id = EXCLUDED.commit_id, published_at = now();")
        stmts.append("COMMIT;")
        compose("exec", "-T", "postgres", "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "caliban", "-d", "caliban",
                input_text="\n".join(stmts))
        info("restarting caliban so the store reloads the ontology")
        compose("restart", "caliban")
        time.sleep(2)
        wait_healthy()
        onto = cp("GET", f"/api/v1/ontology?tenant_id={TENANT}").body
    pending = [e for e in onto["elements"] if e["status"] == "proposed"]
    for e in pending:
        cp("POST", f"/api/v1/ontology/elements/{q(e['id'])}/review",
           {"decision": "approve", "note": "e2e: reviewed against the synthetic marketplace schema"})
    onto = cp("GET", f"/api/v1/ontology?tenant_id={TENANT}").body
    approved = [e for e in onto["elements"] if e["status"] == "approved"]
    st["ontology_version"] = f"{TENANT}@{onto['version']}"
    by_kind = {}
    for e in approved:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    info(f"approved {len(pending)} now, {len(approved)} in total {by_kind}; version {st['ontology_version']}")


def render_spec(st):
    with open(os.path.join(HERE, "node", "triage.node.json"), encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__ONTOLOGY_VERSION__", st["ontology_version"]).replace("__CHAT_MODEL__", CHAT_ID)
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
    step("caliban/auto: intent triage -> node/triage@published")
    t = cp("PATCH", f"/api/v1/tenants/{TENANT}", {"node_routes": {"triage": f"node/{NODE}@published"}}).body
    info(f"node_routes: {t.get('node_routes')}")
    # The data plane applies the change with its next snapshot.
    time.sleep(3)


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
    step("Setup done")
    info(f"tenant {TENANT}, node triage v{st['node_version']}, ontology {st['ontology_version']}")


if __name__ == "__main__":
    main()
