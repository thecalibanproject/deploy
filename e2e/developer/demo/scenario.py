"""Developer scenario: a developer tries Caliban out from code.

A SaaS company's developer (tenant acme-dev, set up by seed.py) gets an API key from the console,
makes a first call with the Python SDK, defines caliban/auto intents for a support app and fixes a
misroute at runtime, then writes three subagents and an orchestrator as code, evaluates them,
passes the promotion gate and routes support traffic to the orchestrator. Every step runs the real
scripts of e2e/developer/app as subprocesses; the terminal card shows their source and their
output, and the console shows the effect. Captions are built from what the scripts and the API
answered.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "lib"))
import dev  # noqa: E402

try:
    from framework import label  # exact label match (a required field's label ends with "*")
except ImportError:  # outside the recorder
    def label(scope, text):
        return scope.get_by_label(text, exact=True)

NAME = "developer"
TITLE = "A developer tries Caliban: intents, then agents"

S = {}  # settings and what the chapters learn, shared across chapters


def chapters(ctx):
    S.update(dev.settings({**os.environ, **(ctx.env or {})}))
    return [
        ("key", key_and_first_call),
        ("intents", intents),
        ("misroute", misroute),
        ("agents", agents),
        ("evals", evals_and_gate),
        ("orchestrator", orchestrator),
        ("usage", usage),
    ]


# ─────────────────────────── helpers ───────────────────────────


def snippet(path, start, count):
    """`count` lines of an app file from the first line matching the regex `start`, under a comment
    naming the file (the card shows an excerpt of the real source, not a command's output)."""
    with open(os.path.join(dev.APP, path), encoding="utf-8") as f:
        lines = f.read().splitlines()
    i = next((n for n, line in enumerate(lines) if re.search(start, line)), 0)
    return [f"# app/{path}, line {i + 1}"] + lines[i:i + count]


def run(ctx, script, *args, label=None):
    """Runs an app script like the developer does; a failure stops the chapter with its output."""
    ctx.mark(f"app: python {script} {' '.join(args)}".strip())
    # A script that sends many requests runs for a while: the stitch keeps its first and last seconds.
    with ctx.idle(f"python {script} {' '.join(args)}".strip()):
        code, out = dev.run_app(S, script, *args, api_key=S.get("api_key"))
    if code != 0:
        ctx.terminal(f"python {script}", [f"$ python {script} {' '.join(args)}"] + out.splitlines()[-25:])
        raise RuntimeError(f"app/{script} {' '.join(args)} exited {code}: {out[-800:]}")
    return out


def term(ctx, title, code_lines, command, output, keep=60):
    lines = list(code_lines)
    if lines:
        lines.append("")
    lines.append(f"$ {command}")
    out = output.splitlines()
    lines += out if len(out) <= keep else out[:keep] + [f"... ({len(out) - keep} more lines)"]
    ctx.terminal(title, lines)


def soft(ctx, what, fn):
    """A console step that only shows something: a selector the console changed must not stop the
    recording; the miss is marked in the timeline."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 (playwright timeouts and selector errors)
        ctx.mark(f"console step skipped ({what}): {type(e).__name__}: {str(e)[:160]}")


def go(ctx, route):
    """Back to the console: the terminal card off first."""
    soft(ctx, "hide the card", ctx.hide_panel)
    ctx.goto(route)


def result(name):
    try:
        with open(os.path.join(S["work"], name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def feed_telemetry(ctx, label, results):
    """The app's requests did not go through ctx.gateway: add them to the framework's telemetry
    records (client wall time and the gateway's Server-Timing, as the script measured them)."""
    try:
        import telemetry
    except ImportError:
        return
    for r in results:
        telemetry.append(NAME, {
            "kind": "request", "scenario": NAME, "chapter": label, "t": ctx.now() if hasattr(ctx, "now") else None,
            "label": f"{label} #{r['n']} {r['lang']} {r['expected']}", "path": "/v1/chat/completions (SDK)",
            "method": "POST", "status": 200,
            "intent": f"{r['intent']};confidence={r['confidence']};stage={r['stage']}" if r.get("intent") else None,
            "route": r.get("target"), "routed_model": r.get("target"), "cache": None, "run_id": r.get("run_id"),
            "client": {"total_ms": round(r["client_ms"], 1) if r.get("client_ms") is not None else None},
            "server": r.get("server_timing") or {}})


def state():
    return dev.load_state(S)


# ─────────────────────────── 1. key and first call ───────────────────────────


def key_and_first_call(ctx):
    """An API key from the console, a first call with the SDK"""
    t = S["tenant"]
    ctx.caption("Get a key", f"The operator gave the developer a tenant, {t}. The key comes from the console.",
                stage="Clear")
    go(ctx, f"/tenants/{t}?tab=api-keys")
    page = ctx.page
    try:
        ctx.type(label(page, "Key name"), "support-app-dev")
        ctx.click(page.get_by_role("button", name="Mint key"))
        code = page.locator("[data-testid=plaintext-key]")
        code.wait_for(timeout=15000)
        S["api_key"] = code.inner_text().strip()
        ctx.caption("Get a key", "Shown once, stored only as its SHA-256: the key resolves to the tenant.",
                    stage="Clear")
        ctx.pause(2)
        soft(ctx, "close the key", lambda: ctx.click(page.get_by_role("button", name="I've stored it")))
    except Exception as e:  # noqa: BLE001
        # Without a key nothing else can run: mint one through the API, and say so.
        ctx.mark(f"console mint failed: {type(e).__name__}")
        ctx.api_caption("Get a key", "The key is minted through the admin API.", "minting an API key", stage="Clear")
        S["api_key"] = ctx.api.post(f"/api/v1/tenants/{t}/api-keys", {"name": "support-app-dev"})["key"]
    dev.save_api_key(S, S["api_key"])

    out = run(ctx, "first_call.py")
    ctx.caption("First call", "Plain chat completions with the Caliban Python SDK, a catalogue model by name.",
                stage="Land")
    term(ctx, "app/first_call.py", snippet("first_call.py", r"^from common", 8), "python first_call.py", out)
    routed = re.search(r"routed model\s+(\S+)", out)
    req = re.search(r"request id\s+(\S+)", out)
    ctx.caption("First call", f"Routed to {routed.group(1) if routed else 'the model'}; the response headers carry "
                f"the cost and the request id {req.group(1)[:16] + '...' if req else ''}.", stage="Land")
    ctx.pause(3)
    go(ctx, f"/usage?tenant={t}")
    ctx.caption("First call", "The console's usage page has the same request: tokens, cost, the routed model.",
                stage="Log")
    soft(ctx, "usage events", lambda: ctx.point(page.get_by_role("table", name="Usage events")))
    ctx.pause(2)


# ─────────────────────────── 2. intents ───────────────────────────


def intents(ctx):
    """Intents from code, fifteen prompts through caliban/auto"""
    t = S["tenant"]
    out = run(ctx, "intents.py", "define")
    ctx.caption("Intents", "Five intents with exemplars in English, French and Arabic, set through the SDK.",
                stage="Route")
    term(ctx, "app/intents.py", snippet("data/intents.json", r'"billing"', 6) + [""] + snippet("intents.py", r"def main", 6),
         "python intents.py define", out)
    ctx.pause(2)
    go(ctx, f"/tenants/{t}?tab=intents")
    ctx.caption("Intents", "The console lists them as set through the API: no config file, no restart.",
                stage="Route")
    soft(ctx, "intents table", lambda: ctx.point(ctx.page.get_by_role("table", name="Routing intents")))
    ctx.pause(2)

    ctx.caption("caliban/auto", "The app sends fifteen prompts through caliban/auto, one after the other.",
                stage="Route")
    out = run(ctx, "classify.py", "first")
    r = result("classify-first.json")
    S["first"] = r
    feed_telemetry(ctx, "classify-first", r.get("results", []))
    bad = [x for x in r.get("results", []) if not x["ok"]]
    ctx.caption("caliban/auto", "Fifteen prompts in three languages, model caliban/auto: kNN over the exemplars "
                "picks the intent.", stage="Route")
    term(ctx, "app/classify.py", snippet("common.py", r"http = httpx.Client", 3) + [""]
         + snippet("classify.py", r"for i, p in enumerate", 6), "python classify.py first", out)
    if bad:
        what = "; ".join(f"#{x['n']} expected {x['expected']}, got {x['intent']} ({x['stage']})" for x in bad[:3])
        ctx.caption("caliban/auto", f"{r.get('ok')} of {r.get('total')} as expected. Misrouted: {what}.",
                    stage="Route", note="x-caliban-intent on every answer: intent, confidence, stage")
    else:
        ctx.caption("caliban/auto", f"{r.get('ok')} of {r.get('total')} routed as expected.", stage="Route",
                    note="x-caliban-intent on every answer: intent, confidence, stage")
    ctx.pause(4)


# ─────────────────────────── 3. a misroute, fixed at runtime ───────────────────────────


def _probe(ctx, text, label):
    body = {"model": "caliban/auto", "messages": [{"role": "user", "content": text}], "max_tokens": 120,
            "caliban": {"cache": "off", "reasoning": "off"}}
    ctx.client_panel("The support app", body, None, {}, None)
    status, headers, resp = ctx.gateway("/v1/chat/completions", body, S["api_key"], label=label)
    ctx.client_panel("The support app", body, status, headers, resp)
    return headers


def misroute(ctx):
    """A misroute, fixed at runtime with new exemplars"""
    first = S.get("first") or result("classify-first.json")
    bad = [x for x in first.get("results", []) if not x["ok"]]
    sales = [x for x in first.get("results", []) if x["expected"] == "sales"]
    probe = next((x for x in bad if x["expected"] == "sales"), bad[0] if bad else sales[0])
    h = _probe(ctx, probe["text"], "classify-before-teach")
    ctx.caption("A misroute" if bad else "The sales intent",
                f"x-caliban-intent: {h.get('x-caliban-intent', 'absent')}", stage="Route")
    ctx.pause(3)
    ctx.hide_panel()

    out = run(ctx, "intents.py", "teach")
    ctx.caption("Teach it", "Growing a team is a sales question: the developer adds sales exemplars, at runtime.",
                stage="Route")
    term(ctx, "app/intents.py teach", snippet("data/intents.teach.json", r'"sales"', 9),
         "python intents.py teach", out)
    ctx.pause(2)
    go(ctx, f"/tenants/{S['tenant']}?tab=intents")
    soft(ctx, "sales row", lambda: ctx.point(ctx.page.get_by_role("row", name=re.compile(r"\bsales\b"))))
    ctx.pause(1)

    h = _probe(ctx, probe["text"], "classify-after-teach")
    ctx.caption("Teach it", f"Same prompt, same gateway, no restart: x-caliban-intent {h.get('x-caliban-intent', 'absent')}",
                stage="Route")
    ctx.pause(3)
    ctx.hide_panel()
    out = run(ctx, "classify.py", "taught")
    r = result("classify-taught.json")
    S["taught"] = r
    feed_telemetry(ctx, "classify-taught", r.get("results", []))
    term(ctx, "app/classify.py", [], "python classify.py taught", out)
    route_ms = sorted(x["route_ms"] for x in r.get("results", []) if x.get("route_ms") is not None)
    timing = (f"Classification p50 {route_ms[len(route_ms) // 2]:.1f} ms, max {route_ms[-1]:.1f} ms (Server-Timing)."
              if route_ms else "This gateway sends no Server-Timing: client times only.")
    ctx.caption("Teach it", f"{first.get('ok')} of {first.get('total')} before, {r.get('ok')} of {r.get('total')} "
                f"after, no restart. {timing}", stage="Route")
    ctx.pause(4)


# ─────────────────────────── 4. agents as code ───────────────────────────


def agents(ctx):
    """Documents and five agents as code, published"""
    t = S["tenant"]
    out = run(ctx, "agents.py", "docs")
    ctx.caption("Agents as code", "docs-helper answers from the product docs: four Markdown pages into a "
                "document collection.", stage="Nodes")
    term(ctx, "app/agents.py docs", snippet("agents.py", r"adm.rag.collections.create", 3) + [""]
         + snippet("agents.py", r"d = adm.rag.documents.ingest", 3), "python agents.py docs", out)
    ctx.pause(2)
    go(ctx, f"/rag?tenant={t}&collection=product_docs")
    ctx.caption("Agents as code", "Indexed by the gateway: structure-aware chunks, hybrid search, scanned for "
                "injections before they are served.", stage="Nodes")
    ctx.pause(2)

    out = run(ctx, "agents.py", "publish")
    ctx.caption("Agents as code", "Three subagents and an orchestrator, written as Python dicts, validated against "
                "the node schema, published.", stage="Nodes")
    term(ctx, "app/support_agents.py", snippet("support_agents.py", r"def support_orchestrator", 20),
         "python agents.py publish", out)
    ctx.pause(3)
    go(ctx, f"/nodes?tenant={t}")
    soft(ctx, "nodes table", lambda: ctx.point(ctx.page.get_by_role("table", name="Nodes")))
    ctx.pause(2)

    out = run(ctx, "agents.py", "gate")
    blocked = "BLOCKED" in out
    ctx.caption("The gate", ("Not live yet: no evals, and every subagent it pins must pass its own gate first."
                             if blocked else "The gate already holds evidence for these versions."), stage="Evals")
    term(ctx, "app/agents.py gate", [], "python agents.py gate", out, keep=20)
    ctx.pause(4)


# ─────────────────────────── 5. evals, gate, promotion, route ───────────────────────────


def evals_and_gate(ctx):
    """Evals, the gate and promotion, children first"""
    t = S["tenant"]
    ctx.caption("Evals", "Golden sets per node (k runs per case), the injection suite for the agent that "
                "reads documents.", stage="Evals", clock=True)
    term(ctx, "app/agents.py evals", snippet("agents.py", r"adm.evals.put_golden_set", 7),
         "python agents.py evals", "(running: every trial is an ordinary node run through the gateway)")
    with ctx.idle("evals"):
        out = run(ctx, "agents.py", "evals")
    st = state()
    for node, suites in (st.get("evals") or {}).items():
        for suite, e in suites.items():
            soft(ctx, f"record eval {node}", lambda e=e, node=node, suite=suite:
                 ctx.record_eval(f"{node} {suite}", {"id": e.get("id"), "suite": suite,
                                                      "status": e.get("status"), "summary": e.get("summary")}))
    term(ctx, "app/agents.py evals", [], "python agents.py evals", out)
    ctx.caption("Evals", "pass^k, cost per success and p95 latency per node, measured on real runs.", stage="Evals")
    ctx.pause(4)
    go(ctx, f"/nodes/{t}/support-orchestrator?tab=evals")
    soft(ctx, "gate card", lambda: ctx.point(ctx.page.get_by_text("Promotion gate").first))
    ctx.pause(2)

    out = run(ctx, "agents.py", "promote")
    term(ctx, "app/agents.py promote", [], "python agents.py promote", out, keep=30)
    ctx.caption("The gate", "Children first: each subagent's own evals, then the orchestrator, whose gate lists "
                "its children's evidence.", stage="Evals")
    ctx.pause(4)

    out = run(ctx, "agents.py", "route")
    term(ctx, "app/agents.py route", snippet("agents.py", r"def route\(", 8), "python agents.py route", out)
    ctx.caption("Route to agents", "Support intents now hand caliban/auto requests to node/support-orchestrator.",
                stage="Route")
    ctx.pause(2)
    go(ctx, f"/tenants/{t}?tab=intents")
    soft(ctx, "hands to", lambda: ctx.point(ctx.page.get_by_role("table", name="Routing intents")))
    ctx.pause(3)


# ─────────────────────────── 6. the orchestrator's run tree ───────────────────────────


def orchestrator(ctx):
    """One message, two problems: the orchestrator and its subagents"""
    t = S["tenant"]
    out = run(ctx, "support.py")
    st = state().get("support_run") or {}
    ctx.caption("Agentic routing", "One message, two problems: the plan splits it, two subagents run in parallel, "
                "one reply.", stage="Nodes")
    term(ctx, "app/support.py", snippet("support.py", r"def main\(message\)", 3), "python support.py", out, keep=40)
    ctx.pause(5)
    run_id = st.get("run_id")
    if run_id:
        soft(ctx, "run timing", lambda: ctx.run_timing(run_id, S["api_key"], label="support.py orchestrator run",
                                                       end_to_end_ms=st.get("client_ms")))
        go(ctx, f"/runs/{t}/{run_id}")
        run_ = ctx.api.get(f"/api/v1/tenants/{t}/runs/{run_id}")
        b = run_.get("budget") or {}
        subs = sorted({s["id"].split(">")[1].split("#")[0] for s in run_.get("steps") or [] if ">" in s["id"]})
        ctx.caption("The run", f"{run_.get('status')}, origin {run_.get('origin')}: subnode depth "
                    f"{b.get('depth_used')} of {b.get('depth_limit')}, fan-out {b.get('fanout_used')}, "
                    f"subagents {', '.join(subs)}.", stage="Nodes",
                    note="Step ids carry the tree: fanout#0/1>bug#0>triage#0")
        soft(ctx, "budget", lambda: ctx.point(ctx.page.get_by_role("table", name="Budget")))
        ctx.pause(2)
        soft(ctx, "steps", lambda: ctx.point(ctx.page.get_by_role("table", name="Steps")))
        ctx.caption("The run", f"Per step: duration, tokens and cost. Billed ${run_.get('cost_usd') or 0:.4f} as "
                    f"caliban/auto; the routed model's real cost ${run_.get('model_cost_usd') or 0:.5f}.",
                    stage="Log")
        ctx.pause(4)

    # The customer app's own view, in French: same path, recorded by the framework's telemetry.
    body = {"model": "caliban/auto", "messages": [{"role": "user", "content":
            "Bonjour, j'ai été prélevé deux fois ce mois-ci et l'export plante dès que je clique dessus."}]}
    ctx.client_panel("The support app", body, None, {}, None)
    status, headers, resp = ctx.gateway("/v1/chat/completions", body, S["api_key"], label="orchestrator-fr")
    ctx.client_panel("The support app", body, status, headers, resp)
    ctx.caption("Agentic routing", f"French, same path: {headers.get('x-caliban-route', headers.get('x-caliban-routed-model', ''))}, "
                f"run {headers.get('x-caliban-run-status', '')}.", stage="Nodes")
    ctx.pause(4)
    ctx.hide_panel()


# ─────────────────────────── 7. usage and budget ───────────────────────────


def usage(ctx):
    """Usage per node and per intent, node spend"""
    t = S["tenant"]
    out = run(ctx, "usage.py")
    term(ctx, "app/usage.py", snippet("usage.py", r"u = raw\(adm", 2), "python usage.py", out, keep=40)
    m = re.search(r"node budget: today (\S+) of (\S+)", out)
    ctx.api_caption("Budget", "Node spend today against the tenant's daily cap" + (f": {m.group(1)} of {m.group(2)}."
                    if m else "."), "the node spend against the caps (GET /tenants/{t}/node-spend)", stage="Log")
    ctx.pause(4)
    go(ctx, f"/usage?tenant={t}")
    ctx.caption("Usage", "Per node and per intent: what each subagent costs, how caliban/auto decided.",
                stage="Log")
    soft(ctx, "cost per node", lambda: ctx.point(ctx.page.get_by_role("region", name="Cost per node")))
    ctx.pause(2)
    soft(ctx, "routing per day", lambda: ctx.point(ctx.page.get_by_role("region", name="Routing per day")))
    ctx.pause(3)
