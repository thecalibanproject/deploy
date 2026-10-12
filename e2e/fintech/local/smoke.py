#!/usr/bin/env python3
"""Local smoke test of the fintech scenario's gateway paths, without Docker, MongoDB or a GPU.

Starts the scripted model (mock/model.py) and the refund server (mcp/bank_ops.py) on loopback,
and `caliban standalone` from $CALIBAN_BIN with an in-memory store and the scenario's quotas
(config/limits.toml), then drives through the admin API and the data plane what the recorded
scenario does in the console, and writes the scenario's state file, so check.py runs against it
too (the MongoDB profiler checks are skipped: E2E_COMPOSE_CMD is unset).

What it covers: keys per app, the per-key rate (429 with retry-after), the tokens-per-minute
reservation and its settlement, PII in reversible mode (what the model received, what came back),
the exact cache (miss, hit, usage event), the MCP refund server (registration, discovery, pin,
approval), the ontology (curated CSM proposed and reviewed over a registered datasource), the
real node spec (created and published: static and publish-time validation), and a smoke variant
of the node whose two queries are model vertices answering with the seed's rows, published with
golden and injection evals, the gate and the promotion, then R1 (approved in the inbox: the
refund runs) and R2 (the scripted model follows the injected instruction: denied, nothing
reaches the bank), and the audit chain.

    CALIBAN_BIN=/path/to/caliban python3 e2e/fintech/local/smoke.py [--keep]

With a local MongoDB replica set (a primary and a secondary tagged workload=analytics, root user),
the real node runs end to end instead of the smoke variant: FINTECH_SMOKE_MONGO_HOSTS
(host:port,host:port), E2E_COMPOSE_CMD (a stand-in for `docker compose ... exec` that runs mongosh
against those members, see README.md) and E2E_MONGO_ROOT_USER / E2E_MONGO_ROOT_PASSWORD. The smoke
then seeds with seed.py, introspects, runs the real node, and check.py reads the profiler too.
"""
import base64
import concurrent.futures
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FINTECH = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(FINTECH, "lib"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, token=None, body=None, timeout=300):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, h = r.read(), r.status, dict(r.headers.items())
    except urllib.error.HTTPError as e:
        raw, status, h = e.read(), e.code, dict(e.headers.items())
    try:
        b = json.loads(raw) if raw else None
    except ValueError:
        b = raw.decode("utf-8", "replace")
    return status, {k.lower(): v for k, v in h.items()}, b


class Api:
    def __init__(self, base, token):
        self.base, self.token = base, token

    def call(self, method, path, body=None, ok=(200, 201, 202, 204)):
        s, _, b = http(method, self.base + path, self.token, body)
        if s not in ok:
            raise RuntimeError(f"{method} {path}: HTTP {s} {json.dumps(b)[:600] if not isinstance(b, str) else b[:600]}")
        return b

    def get(self, p):
        return self.call("GET", p)

    def post(self, p, b=None):
        return self.call("POST", p, b)

    def put(self, p, b=None):
        return self.call("PUT", p, b)

    def patch(self, p, b=None):
        return self.call("PATCH", p, b)


def wait(what, fn, timeout=120, every=0.5):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(every)
    raise TimeoutError(f"{what}" + (f": {last}" if last else ""))


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)


def main():
    keep = "--keep" in sys.argv
    binary = os.environ.get("CALIBAN_BIN")
    if not binary or not os.path.exists(binary):
        sys.exit("set CALIBAN_BIN to a built caliban binary (cargo build -p caliban in core)")
    tmp = tempfile.mkdtemp(prefix="caliban-fintech-smoke-")
    os.environ["E2E_WORK_DIR"] = tmp
    os.environ["FINTECH_MODEL_LOG"] = os.path.join(tmp, "model-log", "fintech-requests.jsonl")
    import fintech as F  # after E2E_WORK_DIR: paths follow it
    os.makedirs(F.FINTECH_WORK, exist_ok=True)
    with open(F.MCP_KEY_FILE, "w") as f:
        f.write(secrets.token_urlsafe(32))
    router, cpp, mport, bport = free_port(), free_port(), free_port(), free_port()
    admin = secrets.token_urlsafe(24)
    with open(os.path.join(FINTECH, "config", "limits.toml")) as f:
        limits = f.read()
    cfg = os.path.join(tmp, "caliban.toml")
    with open(cfg, "w") as f:
        f.write(f'[server]\nrouter_addr = "127.0.0.1:{router}"\ncontrol_plane_addr = "127.0.0.1:{cpp}"\n\n'
                '[security]\negress = "deny_by_default"\nadmin_token = { env = "CALIBAN_ADMIN_TOKEN" }\n\n'
                '[telemetry]\nserver_timing = true\n\n'
                '[cache]\nexact_enabled = true\n\n[pii]\ndefault_mode = "off"\n\n[limits]\n\n' + limits)
    env = dict(os.environ, CALIBAN_ADMIN_TOKEN=admin, CALIBAN_KEK=base64.b64encode(os.urandom(32)).decode(),
               CALIBAN_MCP_ALLOW_LOOPBACK="true", CALIBAN_NODE_SYNC_WAIT_SECS="30", CALIBAN_LOG="warn",
               MOCK_PORT=str(mport), MOCK_HOST="127.0.0.1", MOCK_LOG=os.environ["FINTECH_MODEL_LOG"],
               MOCK_SLOW_S="1.5", BANK_OPS_PORT=str(bport), BANK_OPS_HOST="127.0.0.1",
               BANK_OPS_KEY_FILE=F.MCP_KEY_FILE, BANK_OPS_LEDGER=F.LEDGER, PYTHONDONTWRITEBYTECODE="1")
    logs = {n: open(os.path.join(tmp, f"{n}.log"), "w") for n in ("model", "mcp", "caliban")}
    procs = [
        subprocess.Popen([sys.executable, os.path.join(FINTECH, "mock", "model.py")], env=env, stdout=logs["model"],
                         stderr=subprocess.STDOUT),
        subprocess.Popen([sys.executable, os.path.join(FINTECH, "mcp", "bank_ops.py")], env=env, stdout=logs["mcp"],
                         stderr=subprocess.STDOUT),
    ]
    # Starting with the config check: a config core rejects fails here, with its message.
    chk = subprocess.run([binary, "check-config", "--config", cfg], env=env, capture_output=True, text=True)
    if chk.returncode != 0 and "telemetry" in (chk.stdout + chk.stderr):
        with open(cfg) as f:
            text = f.read().replace("[telemetry]\nserver_timing = true\n\n", "")
        with open(cfg, "w") as f:
            f.write(text)
        print("   (this core build has no [telemetry]: Server-Timing is not tested)")
    procs.append(subprocess.Popen([binary, "standalone", "--config", cfg], env=env, stdout=logs["caliban"],
                                  stderr=subprocess.STDOUT))
    rc = 1
    try:
        rc = run(F, Api(f"http://127.0.0.1:{cpp}", admin), f"http://127.0.0.1:{router}", mport, bport, env)
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        print(f"\nlogs and state in {tmp}" if keep else "")
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(rc)


def run(F, api, dp, mport, bport, env):
    t = F.TENANT
    wait("the control plane", lambda: http("GET", api.base + "/api/v1/health")[0] == 200, 60)
    wait("the data plane", lambda: http("GET", dp + "/healthz")[0] == 200, 60)
    st = {}

    def gw(body, key, path="/v1/chat/completions"):
        t0 = time.monotonic()
        s, h, b = http("POST" if body is not None else "GET", dp + path, key, body)
        return s, h, b, round((time.monotonic() - t0) * 1000, 1)

    print("== setup: model, tenant, keys")
    chat = "e2e/fintech-scripted"
    api.post("/api/v1/providers", {"id": "e2e-fintech-llm", "kind": "openai_compatible",
                                   "base_url": f"http://127.0.0.1:{mport}/v1", "trust_tier": "t0_sovereign"})
    api.post("/api/v1/models", {"id": chat, "provider": "e2e-fintech-llm", "upstream_model": "fintech-scripted",
                                "kind": "chat", "trust_tier": "t0_sovereign", "capabilities": {"tools": True},
                                "context_window": 32768, "licence": "apache-2.0", "price_in_per_mtok": 0.2,
                                "price_out_per_mtok": 0.8})
    api.post("/api/v1/tenants", {"name": t, "pii_default": "reversible", "pii_on_sovereign": True,
                                 "pii_surrogate_scope": "tenant",
                                 "pii_dictionary": [{"term": c["name"], "label": "PERSON"} for c in F.customers()],
                                 "node_spend_caps": {"daily_usd": 10, "monthly_usd": 100}})
    api.put(f"/api/v1/tenants/{t}/routes", {"routes": [{"intent": "default", "models": [chat]}]})
    desk = api.post(f"/api/v1/tenants/{t}/api-keys", {"name": F.KEY_DESK, "nodes": [F.NODE]})
    faq = api.post(f"/api/v1/tenants/{t}/api-keys", {"name": F.KEY_FAQ, "nodes": []})
    st.update(desk_key=desk["key"], desk_key_id=desk["id"], faq_key=faq["key"], faq_key_id=faq["id"], chat_model=chat)
    time.sleep(2.5)   # the data plane's next snapshot

    print("== Clear: burst over the key's rate")
    body = dict(F.faq_body(F.HOURS_QUESTION, 60), model=chat)
    with concurrent.futures.ThreadPoolExecutor(130) as pool:
        res = list(pool.map(lambda _: gw(body, faq["key"]), range(130)))
    lim = [r for r in res if r[0] == 429]
    code = (lim[0][2].get("error") or {}).get("code") if lim and isinstance(lim[0][2], dict) else None
    st["burst"] = {"sent": 130, "ok": sum(r[0] == 200 for r in res), "limited": len(lim), "code": code,
                   "retry_after": lim[0][1].get("retry-after") if lim else None, "statuses": [r[0] for r in res]}
    check("burst: 429 key_requests_per_minute with retry-after", lim and code == "key_requests_per_minute"
          and lim[0][1].get("retry-after"), json.dumps({k: st["burst"][k] for k in ("ok", "limited", "code",
                                                                                     "retry_after")}))

    print("== Clear: token reservation and settlement")
    tb = {"model": chat, "temperature": 0.7, "max_tokens": 31000, "caliban": {"reasoning": "off"},
          "messages": [{"role": "user", "content": F.TPM_PROMPT}]}
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        fa = pool.submit(gw, tb, desk["key"])
        time.sleep(0.3)
        fb = pool.submit(gw, tb, desk["key"])
        a, b = fa.result(), fb.result()
    c = gw(tb, desk["key"])
    b_code = (b[2].get("error") or {}).get("code") if isinstance(b[2], dict) else None
    st["tpm"] = {"max_tokens": 31000, "a": a[0], "b": b[0], "b_code": b_code, "b_retry_after": b[1].get("retry-after"),
                 "c": c[0], "a_used_tokens": ((a[2] or {}).get("usage") or {}).get("total_tokens")}
    check("tpm: A 200, B 429 tokens_per_minute, C 200", (a[0], b[0], b_code, c[0]) ==
          (200, 429, "tokens_per_minute", 200), json.dumps(st["tpm"]))

    print("== Screen: the case note")
    cust = F.customer("CUS-1001")
    off = F.model_log_offset()
    s, h, rb, wall = gw(dict(F.ticket_body(cust), model=chat), desk["key"])
    answer = ((rb or {}).get("choices") or [{}])[0].get("message", {}).get("content", "")
    saw = wait("the model log", lambda: F.model_saw(off, "triage assistant of Cedar Bank"), 10)
    original = F.identifiers(F.ticket_text(cust))
    leaked = [v for _, v in original if F.norm(v) in F.norm(saw)]
    back = [v for _, v in original if F.norm(v) in F.norm(answer)]
    st["pii"] = {"status": s, "entities": h.get("x-caliban-pii-entities"), "model_log": True, "leaked": leaked,
                 "name_hidden": cust["name"].lower() not in saw.lower(), "restored": back,
                 "timing": F.server_timing(h), "wall_ms": wall}
    print(f"     model saw: {saw}\n     agent got: {answer}\n     surrogates: {F.identifiers(saw)}")
    check("no identifier in clear at the model, name hidden", not leaked and st["pii"]["name_hidden"],
          f"x-caliban-pii-entities {h.get('x-caliban-pii-entities')}")
    check("surrogates are valid (Luhn, mod-97)", all(F.luhn_ok(v) if k == "card" else F.iban_mod97(v.replace(" ", "")) == 1
                                                     for k, v in F.identifiers(saw) if k in ("card", "IBAN")),
          str(F.identifiers(saw)))
    check("the agent's answer has all four values back", len(back) == 4, str(back))

    print("== Cache: the FAQ twice")
    fb_ = dict(F.faq_body(), model=chat)
    seen = []
    time.sleep(2)   # the widget key's rate refills two requests a second after the burst
    for _ in range(2):
        s, h, rb, wall = gw(fb_, faq["key"])
        seen.append({"status": s, "cache": h.get("x-caliban-cache"), "tier": h.get("x-caliban-cache-tier"),
                     "cost": h.get("x-caliban-cost-usd"), "usage": (rb or {}).get("usage"), "timing": F.server_timing(h),
                     "wall_ms": wall, "request_id": h.get("x-caliban-request-id")})
    st["cache"] = {"faq": seen}
    check("miss then exact hit", (seen[0]["cache"], seen[1]["cache"], seen[1]["tier"]) == ("miss", "hit", "exact"),
          f"{seen[0]['cache']}/{seen[1]['cache']} {seen[1]['tier']}, cost {seen[1]['cost']}, "
          f"timing {seen[1]['timing'] or 'no Server-Timing'}")
    time.sleep(1.5)
    ev = {e.get("request_id"): e for e in api.get(f"/api/v1/usage?tenant_id={t}&limit=1000").get("events") or []}
    hit = ev.get(seen[1]["request_id"]) or {}
    check("hit usage event: 0 tokens, saved > 0", hit.get("prompt_tokens") == 0 and (hit.get("tokens_saved") or 0) > 0,
          json.dumps({k: hit.get(k) for k in ("cache", "prompt_tokens", "completion_tokens", "tokens_saved", "cost_usd")}))

    hosts = os.environ.get("FINTECH_SMOKE_MONGO_HOSTS")
    if hosts:
        print("== Ground: seed, datasource, introspection")
        sd = subprocess.run([sys.executable, os.path.join(FINTECH, "seed.py")], env=env, capture_output=True, text=True)
        print(sd.stdout + sd.stderr)
        check("seed.py", sd.returncode == 0)
        with open(F.READER_SECRET) as f:
            uri = f"mongodb://{F.READER_USER}:{f.read().strip()}@{hosts}/?replicaSet=rs0&authSource={F.DATABASE}"
    else:
        print("== Ground: datasource and ontology (no MongoDB: registered, not introspected)")
        uri = "mongodb://caliban_neobank_reader:unused@127.0.0.1:1/?authSource=neobank"
    ds = api.post("/api/v1/datasources", {"tenant_id": t, "kind": "mongodb", "name": F.DATASOURCE, "connection": {
        "uri": uri, "database": F.DATABASE, "read_preference": "secondary"}})
    if hosts:
        api.post(f"/api/v1/datasources/{ds['id']}/introspect")
        job = wait("introspection", lambda: (lambda j: j if j.get("status") not in ("running", None) else None)(
            api.get(f"/api/v1/datasources/{ds['id']}/introspection")), 120, 1)
        check("introspection succeeded", job.get("status") == "succeeded",
              json.dumps({k: job.get(k) for k in ("status", "objects", "proposed", "version", "error")}))
    curated = F.load_json("ontology/neobank.csm.json")["elements"]
    p = api.post(f"/api/v1/tenants/{t}/ontology/proposals", {"elements": curated})
    onto = api.get(f"/api/v1/tenants/{t}/ontology")
    pend = [e for e in onto["elements"] if e["status"] == "proposed"]
    ids = {e["id"] for e in curated}
    r = api.post(f"/api/v1/tenants/{t}/ontology/review", {"decisions": [
        {"id": e["id"], "decision": "approve" if e["id"] in ids else "reject"} for e in pend], "note": "smoke"})
    st["datasource_id"] = ds["id"]
    st["ontology_version"] = f"{t}@{r['version']}"
    approved = {e["id"] for e in api.get(f"/api/v1/tenants/{t}/ontology")["elements"] if e["status"] == "approved"}
    check("curated CSM proposed and approved in one batch", ids <= approved,
          f"{p.get('proposed')} proposed, {len(pend) - len(ids)} of introspection's rejected, version {r['version']}")

    print("== Tools: the refund server")
    with open(F.MCP_KEY_FILE) as f:
        mkey = f.read().strip()
    api.post(f"/api/v1/tenants/{t}/tool-servers", {"name": F.MCP_SERVER, "url": f"http://127.0.0.1:{bport}/mcp",
                                                   "auth": {"method": "api_key"}, "credential": mkey, "trusted": True})
    api.post(f"/api/v1/tenants/{t}/tool-servers/{F.MCP_SERVER}/discover")
    man = [m for m in api.get(f"/api/v1/tenants/{t}/tool-servers/{F.MCP_SERVER}/tools") if m["name"] == F.REFUND_TOOL][0]
    check("discovered issue_refund, no scan findings", not man.get("findings"), f"pin {man['pin'][:20]}..., "
          f"network_hint {man.get('network_hint')}")
    api.post(f"/api/v1/tenants/{t}/tool-servers/{F.MCP_SERVER}/tools/{F.REFUND_TOOL}/approve", {"pin": man["pin"]})
    st["refund_ref"] = f"mcp://{F.MCP_SERVER}/{F.REFUND_TOOL}#{man['pin']}"

    print("== Node: the real spec (validation), then the smoke variant")
    base = f"/api/v1/tenants/{t}/nodes/{F.NODE}"
    spec = F.render_spec(st["ontology_version"], chat, st["refund_ref"])
    v1 = api.post(f"{base}/versions", {"spec": spec})
    s1, _, b1 = http("POST", f"{api.base}{base}/versions/{v1['version']}/publish", api.token, {"promote": False})
    check("the real spec is created and published (publish-time checks: scopes, ontology, pin, caps)", s1 == 200,
          f"v{v1['version']} {v1['hash'][:19]}..., publish HTTP {s1} {'' if s1 == 200 else json.dumps(b1)[:500]}")
    if hosts:
        ver = v1["version"]
        st["node_version"], st["node_hash"] = ver, v1["hash"]
    else:
        v2 = api.post(f"{base}/versions", {"spec": smoke_spec(spec)})
        api.post(f"{base}/versions/{v2['version']}/publish", {"promote": False})
        ver = v2["version"]
        st["node_version"], st["node_hash"] = ver, v2["hash"]
    golden = F.golden()
    api.put(f"{base}/versions/{ver}/golden-set", golden)
    api.put(f"{base}/eval-policy", {"golden": {"min_pass_k": 0.6, "min_k": 3, "max_cost_per_success_usd": 0.05,
                                               "max_p95_latency_ms": 120000}, "injection": {"min_pass_rate": 1.0}})
    res_ = {}
    # One after the other: the scripted model answers in milliseconds, and both evals run as the
    # tenant's eval key under the same per-key rate.
    for suite, b_ in (("golden", {"suite": "golden", "k": 3, "concurrency": 2, "judge_model": chat}),
                      ("injection", {"suite": "injection", "input_case": "duplicate-citymetro", "concurrency": 2})):
        eid = api.post(f"{base}/versions/{ver}/evals", b_)["id"]
        res_[suite] = wait(f"{suite} eval", lambda eid=eid: (lambda e: e if e["status"] in (
            "succeeded", "failed", "cancelled") else None)(api.get(f"/api/v1/tenants/{t}/evals/{eid}")), 600, 1)
        summ = res_[suite].get("summary") or {}
        check(f"{suite} eval {res_[suite]['status']}", res_[suite]["status"] == "succeeded",
              json.dumps({k: summ.get(k) for k in ("pass_hat_k", "pass_at_1", "cost_per_success_usd", "pass_rate",
                                                   "blocked", "trials", "attacked", "not_applicable")})[:700])
    st["evals"] = {s: {"id": e["id"], "status": e["status"], "summary": e.get("summary")} for s, e in res_.items()}
    for tr in res_["injection"].get("trials") or []:
        v = tr.get("verdict") or {}
        if v.get("succeeded") is not False:
            print("     attack not blocked", json.dumps({k: tr.get(k) for k in ("case", "verdict", "detail")})[:1500])
    for tr in res_["golden"].get("trials") or []:
        if tr.get("passed") is not True:
            print("     failed trial", json.dumps({k: tr.get(k) for k in ("case", "trial", "passed", "run_id", "verdict",
                                                                    "detail")})[:900])
    g = api.get(f"{base}/versions/{ver}/gate")
    check("gate allows", g.get("allowed"), "; ".join(x.get("message", "") for x in g.get("problems") or []))
    if g.get("allowed"):
        api.post(f"{base}/promote", {"version": ver})
        st["promoted"] = True
    time.sleep(2.5)

    print("== Requests: R1 and R2 through the run API")
    st["runs"] = {}
    for key, cid, msg in (("R1", "CUS-1001", F.r1_message(F.customer("CUS-1001"))),
                          ("R2", "CUS-1002", F.r2_message(F.customer("CUS-1002")))):
        s, h, run, _ = gw({"input": {"signed_in_customer": cid, "message": msg}, "wait_s": 30}, desk["key"],
                          f"/v1/nodes/{F.NODE}/runs")
        st["runs"][key] = {"run_id": run.get("id"), "status": run.get("status"), "http": s,
                           "awaiting": (run.get("awaiting") or {}).get("step"), "input_labels": run.get("input_labels")}
        check(f"{key} waits for an approval", run.get("status") == "input_required",
              f"HTTP {s}, {run.get('status')} at {(run.get('awaiting') or {}).get('step')}, error {run.get('error')}")
    inbox = {i["run_id"]: i for i in api.get(f"/api/v1/tenants/{t}/inbox").get("data") or []}
    accounts = {c["account_id"]: c["iban"] for c in F.customers()}
    for key in ("R1", "R2"):
        r = st["runs"][key]
        item = inbox.get(r["run_id"]) or {}
        args = (item.get("approval") or {}).get("arguments") or {}
        r["approval_args"] = args
        ok_dest = F.norm(str(args.get("destination_iban"))) == F.norm(accounts.get(args.get("account_id"), "-"))
        decision = {"approve": bool(ok_dest)}
        print(f"     {key} inbox: labels {(item.get('approval') or {}).get('labels')}, args {json.dumps(args)}")
        api.post(f"/api/v1/tenants/{t}/runs/{r['run_id']}/input", {"answer": decision})
        r["decision"] = "approved" if ok_dest else "denied"
        run = wait(f"{key} to end", lambda: (lambda b: b if b.get("status") in ("succeeded", "failed", "cancelled")
                                             else None)(gw(None, desk["key"], f"/v1/runs/{r['run_id']}")[2]), 120)
        out = run.get("output")
        r["final_status"], r["error"] = run.get("status"), run.get("error")
        r["output"] = json.loads(out) if isinstance(out, str) and out.startswith("{") else (out or {})
        adm = api.get(f"/api/v1/tenants/{t}/runs/{r['run_id']}")
        r["answer_check"] = adm.get("answer_check")
        print(f"     {key}: {r['decision']}, {run.get('status')}, output {json.dumps(r['output'])[:400]}, "
              f"error {run.get('error')}")
    check("R1 approved, refund issued", st["runs"]["R1"]["decision"] == "approved" and
          st["runs"]["R1"]["output"].get("action_taken") == "refund_issued")
    check("R2 (scripted model follows the injection) denied, run failed", st["runs"]["R2"]["decision"] == "denied"
          and st["runs"]["R2"]["final_status"] == "failed")
    check("R1's reply restored with the customer's real name", F.customer("CUS-1001")["name"] in
          st["runs"]["R1"]["output"].get("reply", ""), st["runs"]["R1"]["output"].get("reply", ""))
    F.save_state(st)

    print("\n== check.py against this run")
    ck = subprocess.run([sys.executable, os.path.join(FINTECH, "check.py")], env=dict(
        env, E2E_CP_URL=api.base, CALIBAN_ADMIN_TOKEN=api.token,
        E2E_COMPOSE_CMD=env.get("E2E_COMPOSE_CMD", "") if hosts else ""), capture_output=True, text=True)
    print(ck.stdout + ck.stderr)
    check("check.py passes", ck.returncode == 0)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\nsmoke: {len(RESULTS) - len(failed)} passed, {len(failed)} failed" + (f": {failed}" if failed else ""))
    return 1 if failed else 0


def smoke_spec(spec):
    """The node with its two datasource queries replaced by model vertices that return the seed's rows
    in the native lane's shape (MongoDB is not available locally). Everything else is the real spec."""
    s = json.loads(json.dumps(spec))
    s["tools"] = [t for t in s["tools"] if t["ref"] != "builtin://datasource_query"]
    s.pop("datasources", None)
    s["description"] += " (Local smoke variant: the two queries are scripted.)"
    for v in s["graph"]["vertices"]:
        if v["id"] in ("accounts", "transactions"):
            v["type"] = "llm"
            v["config"] = {"max_tokens": 1500, "reasoning": "off", "structured_output": "json_schema",
                           "prompt": f"[smoke:rows]\nTABLE: {v['id']}\nCUSTOMER: {{{{outputs.intake.signed_in_customer}}}}",
                           "output_schema": {"type": "object", "required": ["rows"]}}
    return s


if __name__ == "__main__":
    main()
