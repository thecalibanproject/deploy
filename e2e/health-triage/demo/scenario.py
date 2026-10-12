"""Health triage, the reference scenario of the recorded demo (e2e/demo/README.md).

An operator sets the scenario up in the web console exactly as lib/setup.py does through the admin
API, then the marketplace app sends R1 to R7 through caliban/auto (and with E2E_RAG=1 the
retrieval questions G1 to G4) and the console shows what happened to each. Every value comes from
lib/setup.py, lib/run.py and lib/rag.py (the rendered node spec, the PII pattern, the curated
ontology, the exemplars, the documents, the requests), so the recording cannot drift from the e2e.

It replaces `e2e.sh setup`: it writes the same $E2E_WORK_DIR/state.json, so `e2e.sh run` (check.py)
checks everything the console set up.
"""
import json
import os
import re
import sys

import framework
from framework import esc
from playwright.sync_api import expect

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import common  # noqa: E402
from common import TENANT, cp, dp, load_state, save_state, wait_until  # noqa: E402
import rag as e2e_rag  # noqa: E402
import run as e2e_run  # noqa: E402
import setup as e2e_setup  # noqa: E402

NAME = "health-triage"
TITLE = "Health triage for a medical marketplace"

CHAT_ID = e2e_setup.CHAT_ID
NODE = e2e_setup.NODE
TESTBED = os.environ.get("E2E_TARGET") == "testbed"
MODEL_LOG = os.path.join(common.WORK, "model-log", "requests.jsonl")
TERMINAL = framework.TERMINAL_RUN
REQUESTS = {r["id"].split("-")[0]: r for r in e2e_run.REQUESTS}
ST = {}


def chapters(ctx):
    ST.clear()
    ST.update(load_state())
    out = [("models", models), ("tenant", tenant), ("datasource", datasource), ("ontology", ontology),
           ("intents", intents), ("node", node), ("evals", evals), ("apikey", apikey), ("requests", requests)]
    if e2e_rag.ENABLED:
        out.append(("rag", retrieval))
    out.append(("usage-audit", close))
    return out


def models(ctx):
    """The model server and the chat model"""
    tier = os.environ.get("E2E_CHAT_TRUST_TIER", "t0_sovereign")
    base_url = os.environ.get("E2E_CHAT_BASE_URL", "")
    upstream = os.environ.get("E2E_UPSTREAM_MODEL", "e2e-scripted")
    family = os.environ.get("E2E_MODEL_FAMILY") or ""
    page = ctx.page
    ctx.nav("Models")
    ctx.caption("Models", f"Register the server the chat model runs on: {upstream}, reached at {base_url} (the e2e's "
                "recorder, which logs what the model receives), trust tier T0 sovereign: inside the customer's "
                "boundary.", stage="Land", hold=3)
    ctx.click(page.get_by_role("tab", name="Model servers"))
    if "e2e-llm" in {p["id"] for p in cp("GET", "/api/v1/providers").body or []}:
        ctx.caption("Models", "The model server e2e-llm is registered already.", stage="Land", hold=2)
    else:
        ctx.click(page.get_by_role("button", name="Add server"))
        dlg = ctx.dialog("Add model server")
        ctx.type(framework.label(dlg, "Server ID"), "e2e-llm")
        framework.label(dlg, "Kind").select_option("openai_compatible")
        ctx.type(framework.label(dlg, "Base URL"), base_url)
        framework.label(dlg, "Trust tier").select_option(tier)
        if TESTBED:
            ctx.click(framework.label(dlg, "Cache salt"))
            ctx.caption("Models", "Cache salt on: the GPU server's prefix cache is isolated per tenant.",
                        stage="Cache", hold=2.5)
        ctx.click(dlg.get_by_role("button", name="Add server"))
        expect(dlg).to_be_hidden()
    expect(page.get_by_role("table", name="Model servers").get_by_text("e2e-llm", exact=True)).to_be_visible()
    ctx.pause(1.5)

    ctx.click(page.get_by_role("tab", name="Catalogue"))
    if CHAT_ID in {m["id"] for m in cp("GET", "/api/v1/models").body or []}:
        ctx.caption("Models", f"The catalogue entry {CHAT_ID} exists already.", stage="Land", hold=2)
    else:
        ctx.click(page.get_by_role("button", name="Add model"))
        dlg = ctx.dialog("Add model")
        ctx.caption("Models", f"A catalogue entry, {CHAT_ID}: what routes and nodes choose from, with its trust tier, "
                    "capabilities and prices for metering.", stage="Land", hold=1.5)
        ctx.type(framework.label(dlg, "Model ID"), CHAT_ID)
        ctx.type(framework.label(dlg, "Provider"), "e2e-llm")
        ctx.type(framework.label(dlg, "Upstream model"), upstream)
        framework.label(dlg, "Kind").select_option("chat")
        if family:
            ctx.type(framework.label(dlg, "Family"), family)
        framework.label(dlg, "Trust tier").select_option(tier)
        ctx.type(framework.label(dlg, "Licence"), "apache-2.0")
        ctx.type(framework.label(dlg, "Context window"), "32768")
        ctx.type(framework.label(dlg, "Price in (USD / Mtok)"), os.environ.get("E2E_PRICE_IN_PER_MTOK", "0.2"))
        ctx.type(framework.label(dlg, "Price out (USD / Mtok)"), os.environ.get("E2E_PRICE_OUT_PER_MTOK", "0.8"))
        tools = framework.label(dlg, "Tools")
        if not tools.is_checked():
            ctx.click(tools)
        if family.startswith("qwen3"):
            framework.label(dlg, "Reasoning").select_option("hybrid")
            framework.label(dlg, "Reasoning control").select_option("enable_thinking")
            ctx.caption("Models", "Hybrid reasoning, switched per call with enable_thinking: each node vertex says "
                        "whether it thinks.", stage="Land", hold=2.5)
        ctx.click(dlg.get_by_role("button", name="Add model"))
        expect(dlg).to_be_hidden()
    expect(page.get_by_role("table", name="Model registry").get_by_text(CHAT_ID, exact=True)).to_be_visible()
    ctx.pause(1.5)


def tenant(ctx):
    """Tenant, privacy and limits"""
    page = ctx.page
    pii = os.environ.get("E2E_PII_MODE", "mask")
    on_sovereign = os.environ.get("E2E_PII_ON_SOVEREIGN", "0") == "1"
    pat = e2e_setup.PII_PATTERNS[0]
    ctx.nav("Tenants")
    ctx.caption("Tenant", f"Create the tenant {TENANT}: the isolation boundary. Keys, cache, ontology and budgets never "
                "cross it.", stage="Clear", hold=2.5)
    if cp("GET", f"/api/v1/tenants/{TENANT}", ok=None).status != 200:
        ctx.click(page.get_by_role("button", name="New tenant").first)
        dlg = ctx.dialog("New tenant")
        ctx.type(framework.label(dlg, "Name"), TENANT)
        framework.label(dlg, "Default PII handling").select_option(pii)
        ctx.point(framework.label(dlg, "Default PII handling"))
        ctx.click(dlg.get_by_role("button", name="Create tenant"))
        expect(dlg).to_be_hidden()
    ctx.click(page.get_by_role("link", name=f"Open {TENANT}"))
    expect(page.get_by_role("heading", name=TENANT, exact=True)).to_be_visible()

    form = page.get_by_role("form", name="Privacy and caching settings")
    mode = framework.label(form, "PII default mode")
    if mode.input_value() != pii:
        mode.select_option(pii)
    ctx.point(mode, 1.0)
    ctx.caption("Privacy", f"PII mode {pii}: personal data is replaced before a model sees it. Built-in detectors cover "
                "e-mail addresses, cards, IBANs, phone numbers and the French social security number, checked by its "
                "key.", stage="Screen", hold=4)
    sw = framework.label(form, "PII mode on sovereign models")
    if (sw.get_attribute("aria-checked") == "true") != on_sovereign:
        ctx.click(sw)
    ctx.point(sw, 1.0)
    ctx.caption("Privacy", "PII mode on sovereign models: " + (
        "on, so the tenant's in-boundary calls are masked too." if on_sovereign else
        "off, so models inside the boundary get the text as is unless a node sets a PII floor. The triage node "
        "does: every one of its model calls is masked."), stage="Screen", hold=4)
    save = form.get_by_role("button", name="Save")
    if save.is_enabled():
        ctx.click(save)
        expect(form.get_by_role("status")).to_have_text("Settings saved.")

    det = page.get_by_role("form", name="Personal data detectors")
    t = cp("GET", f"/api/v1/tenants/{TENANT}").body
    if not any(p.get("pattern") == pat["pattern"] for p in t.get("pii_patterns") or []):
        det.scroll_into_view_if_needed()
        ctx.caption("Detectors", "Institutional identifiers need the tenant's own rules: a pattern for the Lebanese "
                    "national ID number (LB-4471-9902-3 in the requests).", stage="Screen", hold=1.5)
        ctx.click(det.get_by_role("button", name="Add pattern"))
        n = det.get_by_label(re.compile(r"^Label of pattern \d+$")).count()
        ctx.type(framework.label(det, f"Label of pattern {n}"), pat["label"])
        ctx.type(framework.label(det, f"Pattern {n}"), pat["pattern"])
        ctx.click(det.get_by_role("button", name="Save"))
        expect(det.get_by_role("status")).to_have_text("Detectors saved.")
    ctx.point(det, 1.5)

    lim = page.get_by_role("form", name="Node limits")
    caps = t.get("node_spend_caps") or {}
    if caps.get("daily_usd") != 20 or caps.get("monthly_usd") != 200:
        lim.scroll_into_view_if_needed()
        ctx.caption("Limits", "Node spend caps: what the tenant's node runs may spend on model calls, 20 USD a day and "
                    "200 a month, checked before every model call.", stage="Clear", hold=1.5)
        ctx.type(framework.label(lim, "Daily spend cap (USD)"), "20")
        ctx.type(framework.label(lim, "Monthly spend cap (USD)"), "200")
        ctx.click(lim.get_by_role("button", name="Save"))
        expect(lim.get_by_role("status")).to_have_text("Limits saved.")
    ctx.pause(1.5)

    cp("PUT", f"/api/v1/tenants/{TENANT}/routes", {"routes": [{"intent": "default", "models": [CHAT_ID]}]})
    ctx.api_caption("Routes", f"The tenant's default route sends what no intent claims to {CHAT_ID}.",
                    "PUT /api/v1/tenants/{t}/routes", stage="Route")
    t = cp("GET", f"/api/v1/tenants/{TENANT}").body
    ST["tenant_settings"] = {k: t.get(k) for k in ("pii_default", "pii_on_sovereign", "pii_patterns")}
    save_state(ST)


def datasource(ctx):
    """MongoDB datasource and introspection"""
    page = ctx.page
    ctx.nav("Datasources")
    ctx.caption("Datasource", "Connect the marketplace's MongoDB replica set with a read-only principal: Caliban refuses "
                "a user that holds any write or admin action. The credentials are sealed and never shown again.",
                stage="Ground", hold=3)
    live = [x for x in cp("GET", f"/api/v1/datasources?tenant_id={TENANT}").body if x["name"] == "health"]
    if not live:
        ctx.click(page.get_by_role("button", name="Register datasource").first)
        form = page.get_by_role("form", name="Register datasource")
        expect(form).to_be_visible()
        framework.label(form, "Tenant").select_option(TENANT)
        framework.label(form, "Kind").select_option("mongodb")
        ctx.type(framework.label(form, "Name"), "health")
        uri = os.environ.get("DEMO_MONGO_URI") or (
            f"mongodb://caliban_reader:{os.environ['E2E_MONGO_READER_PASSWORD']}@caliban-e2e-mongo:27017,"
            "caliban-e2e-mongo-analytics:27017/?replicaSet=rs0&authSource=marketplace")
        field = framework.label(form, "Connection URI")
        ctx.point(field)
        field.fill(uri)                       # a password field: the video shows dots only
        ctx.type(framework.label(form, "Database"), "marketplace")
        # The console always sends a read preference: `secondary` keeps every query off the primary,
        # like the compiled default (the tagged analytics secondary, the only secondary).
        framework.label(form, "Read preference").select_option(os.environ.get("DEMO_READ_PREFERENCE", "secondary"))
        ctx.click(form.get_by_role("button", name="Register datasource"))
        expect(form).to_be_hidden()
        live = [x for x in cp("GET", f"/api/v1/datasources?tenant_id={TENANT}").body if x["name"] == "health"]
    ds = ST["datasource_id"] = live[0]["id"]
    row = page.get_by_role("table", name="Datasources").get_by_role("row").filter(has_text="health")
    expect(row).to_be_visible()
    ctx.caption("Introspection", "Introspect: the control plane samples every collection through the reader and "
                "proposes the entities, attributes and relations it infers, for a person to review.", stage="Ground",
                hold=1)
    ctx.click(row.get_by_role("button", name="Introspect health"))
    timeout = int(os.environ.get("E2E_INTROSPECT_TIMEOUT_S", "300"))
    with ctx.idle("introspection"):
        try:
            expect(row.get_by_text(re.compile(r"^(succeeded|failed)$"))).to_be_visible(timeout=timeout * 1000)
        except AssertionError:
            pass
    job = cp("GET", f"/api/v1/datasources/{ds}/introspection").body or {}
    ST["introspection"] = {k: job.get(k) for k in ("id", "status", "objects", "proposed", "skipped", "version", "error")}
    if job.get("status") == "succeeded":
        ctx.caption("Introspection", f"Introspection succeeded: {job.get('objects')} collections, {job.get('proposed')} "
                    f"elements proposed, in ontology version {job.get('version')}.", stage="Ground", hold=3.5)
    else:
        ctx.caption("Introspection", f"Introspection ended {job.get('status')}: {job.get('error') or 'see the row'}. The "
                    "curated model alone is enough.", stage="Ground", hold=3.5)
    save_state(ST)


def unique_names(elements):
    seen = {}
    for e in elements:
        seen[e["name"]] = seen.get(e["name"], 0) + 1
    return {n for n, c in seen.items() if c == 1}


def ontology(ctx):
    """Ontology review"""
    page = ctx.page
    path = f"/api/v1/tenants/{TENANT}/ontology"
    with open(os.path.join(common.HERE, "ontology", "health.csm.json"), encoding="utf-8") as f:
        curated = {e["id"]: e for e in json.load(f)["elements"]}
    ctx.goto(f"/ontology?tenant={TENANT}")
    expect(page.get_by_role("heading", name="Ontology studio")).to_be_visible()
    ctx.caption("Ontology", "The Caliban Semantic Model: a reviewed map of the data. Node queries name its ids only; "
                "nothing a reviewer has not approved can be queried.", stage="Ground", hold=3)
    current = {e["id"]: e for e in cp("GET", path).body["elements"]}
    fresh = [e for i, e in curated.items()
             if not (current.get(i, {}).get("status") == "approved" and current[i].get("kind") == e["kind"]
                     and e2e_setup.covers(current[i].get("spec"), e["spec"]))]
    if fresh:
        ctx.caption("Ontology", f"Propose the curated model over introspection's: {len(fresh)} elements with the "
                    "marketplace's own names, its metrics, a relation and glossary terms in three languages.",
                    stage="Ground", hold=1.5)
        ctx.click(page.get_by_role("button", name="Propose elements"))
        dlg = ctx.dialog("Propose elements")
        box = framework.label(dlg, "Elements (JSON)")
        ctx.point(box)
        box.fill(json.dumps(fresh, indent=2, ensure_ascii=False))
        ctx.pause(1.5)
        ctx.click(dlg.get_by_role("button", name="Propose", exact=True))
        expect(dlg.get_by_role("status")).to_have_text(re.compile(r"^(Wrote \d+ proposals?|Nothing written)"))
        ctx.pause(2.5)
        ctx.click(dlg.get_by_role("button", name="Close", exact=True).last)
        expect(dlg).to_be_hidden()

    pending = [e for e in cp("GET", path).body["elements"] if e["status"] == "proposed"]
    names = unique_names(pending)
    approve_ui = [e for e in pending if e["id"] in curated and e["name"] in names][:3]
    reject_ui = [e for e in pending if e["id"] not in curated and e["name"] in names][:2]
    queue = page.get_by_role("table", name="Review queue")
    if approve_ui or reject_ui:
        expect(queue).to_be_visible()
        ctx.caption("Review", "Approve the curated elements, reject introspection's own names for the same paths. Each "
                    "decision is audited with the reviewer's name.", stage="Ground", hold=1.5)
    for verb, items in (("Approve", approve_ui), ("Reject", reject_ui)):
        for e in items:
            btn = queue.get_by_role("button", name=f"{verb} {e['name']}", exact=True)
            ctx.click(btn)
            expect(btn).to_be_hidden()

    onto = cp("GET", path).body
    before = onto["version"]
    pending = [e for e in onto["elements"] if e["status"] == "proposed"]
    decisions = [{"id": e["id"], "decision": "approve" if e["id"] in curated else "reject"} for e in pending]
    if decisions:
        r = cp("POST", f"{path}/review", {"decisions": decisions,
                                          "note": "demo: reviewed against the synthetic marketplace schema"}).body
        n_ok = sum(1 for x in decisions if x["decision"] == "approve")
        ST["review_batch"] = {"decisions": len(decisions), "from": before, "to": r["version"]}
        ctx.api_caption("Review", f"The other {len(decisions)} decisions ({n_ok} approvals, {len(decisions) - n_ok} "
                        f"rejections) in one review batch: one new ontology version, {before} to {r['version']}.",
                        "POST /api/v1/tenants/{t}/ontology/review (the console reviews one element at a time)",
                        stage="Ground")
        page.reload()
    onto = cp("GET", path).body
    approved = [e for e in onto["elements"] if e["status"] == "approved"]
    missing = sorted(set(curated) - {e["id"] for e in approved})
    if missing:
        raise RuntimeError(f"curated elements not approved: {missing}")
    ST["ontology_version"] = f"{TENANT}@{onto['version']}"
    save_state(ST)
    expect(page.get_by_text(re.compile(rf"^Version {onto['version']} · \d+ elements$"))).to_be_visible()
    page.get_by_role("heading", name="Elements by kind").scroll_into_view_if_needed()
    ctx.caption("Ontology", f"{len(approved)} approved elements, version {ST['ontology_version']}. The model picks, the "
                "compiler writes: queries are typed CQIR over these ids, lowered by a Rust compiler to a MongoDB "
                "pipeline.", stage="Ground", hold=4)


def intents(ctx):
    """Routing intents"""
    page = ctx.page
    with open(os.path.join(common.HERE, "config", "exemplars.json"), encoding="utf-8") as f:
        wanted = json.load(f)["intents"]
    ctx.goto(f"/tenants/{TENANT}?tab=intents")
    expect(page.get_by_role("tab", name="Routing intents")).to_have_attribute("aria-selected", "true")
    ctx.caption("Routing intents", "caliban/auto classifies each request by its nearest exemplars, in English, French "
                "and Arabic. Changes reach the routers without a restart.", stage="Route", hold=3)
    listed = {i["intent"]: i for i in (cp("GET", f"/api/v1/tenants/{TENANT}/intents").body.get("intents") or [])}
    for name, exemplars in wanted.items():
        have = listed.get(name)
        if have and have.get("source") == "api" and have.get("exemplars") == exemplars:
            continue
        if have:
            ctx.click(page.get_by_role("table", name="Routing intents").get_by_role("button", name=name, exact=True))
            form = page.get_by_role("form", name=f"Intent {name}")
        else:
            ctx.click(page.get_by_role("button", name="New intent"))
            form = page.get_by_role("form", name="New intent")
            ctx.type(framework.label(form, "Intent"), name)
        box = framework.label(form, "Exemplars")
        ctx.point(box)
        box.fill("\n".join(exemplars))
        ctx.pause(2.5)
        ctx.click(form.get_by_role("button", name="Save"))
        expect(form).to_be_hidden()
    row = page.get_by_role("table", name="Routing intents").get_by_role("row").filter(has_text="triage")
    expect(row).to_be_visible()
    ctx.point(row, 2)


def node_graph_html(spec):
    kinds = {"verify": "check", "llm": "model", "human": "human step", "tool": "query"}
    vs = spec["graph"]["vertices"]
    query = next(((v.get("config") or {}).get("args") for v in vs if v.get("kind") == "tool"), {})
    chips = " → ".join(f'<span class="badge">{esc(v["id"])} · {esc(kinds.get(v.get("kind"), v.get("kind")))}</span>'
                       for v in vs)
    edges = "<br>".join(f'{esc(e["from"])} → {esc(e["to"])}'
                        + (f' <span class="muted">when {esc(e["when"])}</span>' if e.get("when") else "")
                        for e in spec["graph"]["edges"])
    g, mp = spec.get("guardrails") or {}, spec.get("model_policy") or {}
    return ('<div class="card"><div class="head"><span class="t">Node triage: the graph in its spec</span>'
            '<span class="s">drawn by the demo from the version\'s JSON; the console shows the spec and its '
            'behaviour, not a diagram</span></div><div class="cols"><div class="col">'
            f'<h4>Vertices</h4><div style="line-height:2.4">{chips}</div><h4>Edges</h4><pre>{edges}</pre></div>'
            '<div class="col"><h4>Guardrails and model policy</h4><table>'
            f'<tr><td class="k">guardrails.pii</td><td>{esc(g.get("pii"))}: a floor on every model call</td></tr>'
            f'<tr><td class="k">guardrails.answer</td><td>{esc(g.get("answer"))}: the answer guard</td></tr>'
            f'<tr><td class="k">model_policy.reasoning</td><td>{esc(mp.get("reasoning"))}</td></tr>'
            f'<tr><td class="k">budgets</td><td>{esc(json.dumps(spec.get("budgets")))}</td></tr></table>'
            '<h4>A catalogue query (typed CQIR, ids from the ontology)</h4>'
            f'<pre>{esc(json.dumps(query, indent=1, ensure_ascii=False)[:900])}</pre></div></div></div>')


def node(ctx):
    """The triage node: import, behaviour, publish"""
    page = ctx.page
    spec = e2e_setup.render_spec(ST)
    base = f"/api/v1/tenants/{TENANT}/nodes/{NODE}"
    listed = cp("GET", f"{base}/versions", ok=None)
    existing = listed.body.get("versions", []) if listed.status == 200 else []
    ctx.nav("Nodes")
    ctx.caption("Node", "A node is a versioned, budgeted AI sub-app: config, not code. Import the triage node's spec, "
                "checked live against the node schema.", stage="Nodes", hold=3)
    if not [v for v in existing if v["spec"] == spec and v["state"] != "retired"]:
        if existing:
            ctx.goto(f"/nodes/{TENANT}/{NODE}")
            ctx.click(page.get_by_role("button", name="New version"))
            form = page.get_by_role("form", name="Create version")
        else:
            ctx.click(page.get_by_role("button", name="New node").first)
            form = page.get_by_role("form", name="Create node")
            framework.label(form, "Tenant").select_option(TENANT)
            ctx.type(framework.label(form, "Name"), NODE)
        box = form.locator("#node-spec")
        ctx.point(box)
        box.fill(json.dumps(spec, indent=2, ensure_ascii=False))
        expect(form.get_by_text("Valid against node.schema.json")).to_be_visible()
        ctx.pause(2)
        ctx.click(form.get_by_role("button", name=re.compile(r"^Create (node|draft)$")))
        expect(form).to_be_hidden()
    else:
        ctx.goto(f"/nodes/{TENANT}/{NODE}")
    expect(page.get_by_role("table", name="Versions")).to_be_visible()
    v = [x for x in cp("GET", f"{base}/versions").body["versions"] if x["spec"] == spec and x["state"] != "retired"][-1]
    ver = v["version"]

    ctx.panel(node_graph_html(spec))
    ctx.caption("Node", "intake checks the input, assess classifies the case, clarify asks the patient once if needed, "
                "three catalogue queries run with values from assess only, recommend answers in the node's JSON "
                "schema.", stage="Nodes", hold=7)
    ctx.hide_panel()
    region = page.get_by_role("region", name="Answer and model calls")
    if region.count():
        ctx.point(region.first, 1)
        ctx.caption("Behaviour", "Answer guard redact: links, contacts and echoed instructions that only untrusted text "
                    "supplied are removed. PII floor mask on every model call, sovereign models included. Both model "
                    "vertices: thinking off, constrained decoding to their JSON schema.", stage="Screen", hold=6)
    ctx.click(page.get_by_role("button", name=f"View spec of v{ver}"))
    spec_dlg = page.get_by_role("dialog", name=f"{NODE} v{ver}")
    expect(spec_dlg).to_be_visible()
    ctx.caption("Spec", f"The queries name ontology version {ST['ontology_version']}, the one the reviewer just "
                "approved. The model never writes a query.", stage="Ground", hold=4)
    page.keyboard.press("Escape")
    expect(spec_dlg).to_be_hidden()

    if v["state"] == "draft":
        ctx.click(page.get_by_role("button", name=f"Publish v{ver}"))
        dlg = ctx.dialog(f"Publish {NODE} v{ver}?")
        box = framework.label(dlg, "Make it the live version")
        if box.is_checked():
            ctx.click(box)
        ctx.caption("Publish", "Publish without making it live: the promotion bar needs evals first. The control plane "
                    "checks scopes, caps and pins, then ships the version sealed.", stage="Nodes", hold=1.5)
        ctx.click(dlg.get_by_role("button", name="Publish", exact=True))
        expect(dlg).to_be_hidden()
    v = [x for x in cp("GET", f"{base}/versions").body["versions"] if x["version"] == ver][0]
    ST["node_version"], ST["node_hash"] = v["version"], v["hash"]
    save_state(ST)
    ctx.pause(1.5)


def evals(ctx):
    """Evals, the gate and promotion"""
    page = ctx.page
    ver = ST["node_version"]
    base = f"/api/v1/tenants/{TENANT}/nodes/{NODE}"
    with open(os.path.join(common.HERE, "node", "golden.json"), encoding="utf-8") as f:
        golden = json.load(f)
    p95 = os.environ.get("E2E_MAX_P95_MS", "60000")
    min_pass = float(os.environ.get("E2E_MIN_PASS_K", "0.8"))
    ctx.goto(f"/nodes/{TENANT}/{NODE}?tab=evals&ev={ver}")
    expect(page.get_by_role("tab", name="Evals")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name=f"Evals of v{ver}")).to_be_visible()
    ctx.caption("Evals", "Before a version goes live it must meet the node's promotion bar: golden cases run k times "
                "each, and the prompt injection suite.", stage="Nodes", hold=2.5)
    ctx.click(page.get_by_role("button", name="Edit bar"))
    dlg = ctx.dialog(f"Promotion bar of {NODE}")
    ctx.type(framework.label(dlg, "Least pass^k"), f"{min_pass * 100:g}")
    ctx.type(framework.label(dlg, "Least k"), "3")
    ctx.type(framework.label(dlg, "Most cost per success"), "0.05")
    ctx.type(framework.label(dlg, "Most p95 latency"), p95)
    ctx.type(framework.label(dlg, "Least share of attacks blocked"), "100")
    framework.label(dlg, "Read-only versions").select_option("required")
    ctx.caption("Promotion bar", f"pass^k at least {min_pass:.0%} over k = 3, cost per success at most 0.05 USD, p95 at "
                f"most {int(p95) // 1000} s, every attack blocked, and an injection eval although the node only "
                "reads.", stage="Nodes", hold=2)
    ctx.click(dlg.get_by_role("button", name="Save bar"))
    expect(dlg).to_be_hidden()

    ctx.caption("Golden set", f"{len(golden['cases'])} golden cases in English, French and Arabic, two with a scripted "
                "human answer for the clarifying question.", stage="Nodes", hold=1)
    ctx.click(page.get_by_role("button", name=re.compile(r"^(Create golden set|Edit cases)$")))
    dlg = ctx.dialog(f"Golden set of {NODE} v{ver}")
    box = framework.label(dlg, "Cases")
    ctx.point(box)
    box.fill(json.dumps(golden["cases"], indent=2, ensure_ascii=False))
    ctx.pause(1.5)
    ctx.click(dlg.get_by_role("button", name="Save golden set"))
    expect(dlg).to_be_hidden()
    expect(page.get_by_role("table", name="Golden cases")).to_be_visible()

    started = {}
    for suite in ("golden", "injection"):
        ctx.click(page.get_by_role("button", name="Start eval").first)
        dlg = ctx.dialog(f"Start an eval of {NODE} v{ver}")
        framework.label(dlg, "Suite").select_option(suite)
        if suite == "golden":
            ctx.type(framework.label(dlg, "k (runs per case)"), "3")
            ctx.type(framework.label(dlg, "Concurrency"), "4")
            ctx.type(framework.label(dlg, "Judge model"), CHAT_ID)
            ctx.caption("Golden eval", "Every case three times, as ordinary node runs through the gateway, metered as "
                        "the tenant's eval key.", stage="Nodes", hold=1)
        else:
            framework.label(dlg, "Run input").select_option("en-chest-beirut")
            ctx.caption("Injection suite", "The suite attacks the node through what it reads, on a golden case's "
                        "input, and measures what still works under attack.", stage="Nodes", hold=1)
        before = {e["id"] for e in (cp("GET", f"/api/v1/tenants/{TENANT}/evals").body.get("data") or [])}
        ctx.click(dlg.get_by_role("button", name="Start eval"))
        expect(dlg).to_be_hidden()
        new = wait_until("the eval to be listed", lambda: [e for e in (cp(
            "GET", f"/api/v1/tenants/{TENANT}/evals").body.get("data") or []) if e["id"] not in before], timeout=30)
        started[suite] = new[0]["id"]
        ctx.pause(2)
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name=f"Eval {started[suite]}")).to_be_hidden()
    ctx.caption("Evals", "Both evals are running: each trial is a real node run with its model calls, catalogue queries "
                "and checks.", stage="Nodes", clock=True)
    results = {}
    timeout = int(os.environ.get("E2E_EVAL_TIMEOUT_S", "1800"))
    with ctx.idle("evals"):
        for suite, eid in started.items():
            results[suite] = wait_until(f"the {suite} eval", lambda eid=eid: (
                lambda b: b if b["status"] in ("succeeded", "failed", "cancelled") else None)(
                cp("GET", f"/api/v1/tenants/{TENANT}/evals/{eid}").body), timeout=timeout, every=2)
    for suite, e in results.items():
        ctx.record_eval(f"triage v{ver} {suite}", e)
    ST["evals"] = {s: {"id": e["id"], "status": e["status"], "summary": e.get("summary"),
                       "trials": [{k: t.get(k) for k in ("case", "trial", "passed", "run_id", "verdict")}
                                  for t in e.get("trials", [])]} for s, e in results.items()}
    save_state(ST)
    page.reload()
    table = page.get_by_role("table", name="Evals")
    expect(table).to_be_visible()
    ctx.point(table, 1)
    gs, isum = results["golden"].get("summary") or {}, results["injection"].get("summary") or {}
    ctx.caption("Evals", f"Golden: {results['golden']['status']}, pass^k {gs.get('pass_hat_k')}, p95 "
                f"{(gs.get('latency_ms') or {}).get('p95')} ms. Injection: {results['injection']['status']}, "
                f"{isum.get('blocked')} of {isum.get('trials')} attacks blocked.", stage="Nodes", hold=3)
    ctx.click(table.get_by_role("button", name=f"Open eval {started['injection']}"))
    drawer = page.get_by_role("dialog", name=f"Eval {started['injection']}")
    expect(drawer).to_be_visible()
    ctx.pause(4)
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()

    g = ST["gate"] = cp("GET", f"{base}/versions/{ver}/gate").body
    save_state(ST)
    badge = page.get_by_text(re.compile(r"^\s*(Meets the bar|Blocked by the promotion bar)\s*$"))
    expect(badge).to_be_visible()
    ctx.point(badge, 1)
    if not g.get("allowed"):
        ctx.caption("Gate", "The gate refuses: " + "; ".join(p.get("message", "") for p in g.get("problems") or []),
                    stage="Nodes", hold=6)
        ST["promoted"] = False
        save_state(ST)
        raise RuntimeError(f"the gate refused v{ver}: {json.dumps(g)[:800]}")
    ctx.caption("Gate", "The latest golden and injection evals of this version meet the bar.", stage="Nodes", hold=2)
    if cp("GET", f"{base}/versions").body.get("live") != ver:
        ctx.click(page.get_by_role("button", name=f"Make v{ver} live").first)
        dlg = ctx.dialog(f"Make {NODE} v{ver} live?")
        ctx.click(dlg.get_by_role("button", name="Make live"))
        expect(dlg).to_be_hidden()
    expect(page.get_by_text(f"Live: v{ver}")).to_be_visible()
    ST["promoted"] = True
    save_state(ST)
    ctx.pause(2)

    cp("PATCH", f"/api/v1/tenants/{TENANT}", {"node_routes": {"triage": f"node/{NODE}@published"}})
    ctx.goto(f"/tenants/{TENANT}?tab=intents")
    row = page.get_by_role("table", name="Routing intents").get_by_role("row").filter(has_text="triage")
    expect(row.get_by_role("link", name=f"node/{NODE}@published")).to_be_visible()
    ctx.point(row, 0.5)
    ctx.api_caption("Node route", "The triage intent now hands to node/triage@published, the live version.",
                    "PATCH /api/v1/tenants/{t} node_routes (the console shows the mapping, cannot edit it)",
                    stage="Route")
    listed = cp("GET", f"/api/v1/tenants/{TENANT}/intents").body
    ST["intents"] = {i["intent"]: {"exemplars": len(i.get("exemplars") or []), "source": i.get("source"),
                                   "node": i.get("node")} for i in listed.get("intents") or []}
    save_state(ST)


def mint_key(ctx, name, nodes, scopes=None, caption=None):
    """Mints an application key on the tenant's API keys tab; returns (key, id). The key is blurred."""
    page = ctx.page
    ctx.goto(f"/tenants/{TENANT}?tab=api-keys")
    expect(page.get_by_role("tab", name="API keys")).to_have_attribute("aria-selected", "true")
    page.add_style_tag(content="[data-testid=plaintext-key]{filter:blur(7px)}")
    if caption:
        ctx.caption(*caption[:2], stage=caption[2], hold=2.5)
    ctx.type(framework.label(page, "Key name"), name)
    ctx.click(page.locator("summary").filter(has_text="Node access"))
    ctx.click(framework.label(page, "Only these nodes"))
    group = page.get_by_role("group", name="Nodes", exact=True)
    expect(group.or_(framework.label(page, "Node names")).first).to_be_visible()
    for n in nodes:
        box = group.get_by_label(n, exact=True)
        if group.count() and box.count():
            ctx.click(box)
        else:
            ctx.type(page.get_by_label(re.compile(r"^(Other node names|Node names)$")), ", ".join(nodes))
            break
    if scopes:
        ctx.type(framework.label(page, "Datasource scopes"), ", ".join(scopes))
    ctx.click(page.get_by_role("button", name="Mint key"))
    notice = page.get_by_role("region", name="New API key")
    expect(notice).to_be_visible()
    key = notice.get_by_test_id("plaintext-key").inner_text().strip()
    ctx.point(notice, 2.5)
    keys = cp("GET", f"/api/v1/tenants/{TENANT}/api-keys").body
    match = [k for k in keys if k.get("name") == name and key.startswith(k.get("prefix") or "\0")]
    ctx.click(notice.get_by_role("button", name="I've stored it"))
    return key, (match[-1]["id"] if match else None)


def apikey(ctx):
    """An API key for the marketplace app"""
    key, kid = mint_key(ctx, "medmarket-app", [NODE], caption=(
        "API key", "An application key for the marketplace app, allowed to run the triage node only. Caliban stores "
        "only its SHA-256 hash; the key is blurred in this recording.", "Clear"))
    ST.update(api_key=key, api_key_id=kid)
    save_state(ST)
    ctx.pause(1.5)


# ─────────────────────────── the requests ───────────────────────────


def model_log_offset():
    return os.path.getsize(MODEL_LOG) if os.path.exists(MODEL_LOG) else 0


def model_saw(offset, marker):
    """The user message of the first model call after `offset` that carries `marker` (the e2e
    recorder's log), or None."""
    if not os.path.exists(MODEL_LOG):
        return None
    with open(MODEL_LOG, encoding="utf-8") as f:
        f.seek(offset)
        lines = f.read().splitlines()
    for line in lines:
        try:
            msgs = json.loads(line).get("messages") or []
        except ValueError:
            continue
        text = "\n".join(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
                         for m in msgs)
        if marker in text:
            users = [m.get("content") for m in msgs if m.get("role") == "user" and isinstance(m.get("content"), str)]
            return (users[-1] if users else text)[-900:]
    return None


def answer_details(parsed):
    if not isinstance(parsed, dict):
        return ""
    rows = [(k, parsed[k]) for k in ("specialty", "urgency", "language") if parsed.get(k)]
    fac = parsed.get("facility") or {}
    if fac:
        rows.append(("facility", f"{fac.get('name') or ''} ({fac.get('code')}, {fac.get('city')})"))
    if parsed.get("services"):
        rows.append(("services", ", ".join(str(s.get("code")) for s in parsed["services"])))
    return ("<h4>Structured answer</h4><table>" + "".join(
        f'<tr><td class="k">{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in rows) + "</table>") if rows else ""


def follow(key, run_id):
    return wait_until(f"run {run_id}", lambda: (lambda r: r if r["status"] in TERMINAL else None)(
        dp("GET", f"/v1/runs/{run_id}", key).body), timeout=int(os.environ.get("E2E_RUN_TIMEOUT_S", "600")), every=1.0)


def show_run(ctx, run_id, lines, outputs=(), ended=True):
    """Opens a run in the console and walks the viewer through it (`ended`: wait until the page
    shows it ended; False for a run that waits for a human)."""
    page = ctx.page
    ctx.hide_panel()
    ctx.goto(f"/runs/{TENANT}/{run_id}")
    # The page shows this run (not the one shown before) before anything is said about it.
    expect(page.get_by_role("heading", level=1, name=run_id, exact=True)).to_be_visible()
    steps = page.get_by_role("table", name="Steps")
    expect(steps).to_be_visible()
    if ended:
        expect(page.get_by_text("Refreshing while the run is not ended.")).to_be_hidden(timeout=120000)
    else:
        expect(page.get_by_text("Waiting for an answer")).to_be_visible()
    targets = {"billed": page.get_by_text("Model cost", exact=True), "pii": page.get_by_text("Personal data", exact=True),
               "guard": page.get_by_text(re.compile(r"^Answer guard")), "steps": steps}
    for stage, line, target in lines:
        loc = targets.get(target)
        if loc is not None and loc.count():
            ctx.point(loc.first, 0.6)
        ctx.caption(f"Run {run_id[:18]}", line, stage=stage, hold=4)
    for vertex in outputs:
        btn = steps.get_by_role("button", name=f"Show output of {vertex}")
        if btn.count():
            ctx.click(btn.first)
            ctx.pause(3)


def send_chat(ctx, rid, title_suffix=""):
    spec = REQUESTS[rid]
    key = ST["api_key"]
    body = {"model": "caliban/auto", "messages": [{"role": "user", "content": spec["text"]}]}
    title = f"{spec['id']}: {spec['title']}{title_suffix}"
    rtl = rid == "R4"
    ctx.client_panel(title, body, None, None, None, pending="sending to Caliban…")
    ctx.pause(2.5)
    off = model_log_offset()
    status, h, b = ctx.gateway("/v1/chat/completions", body, key, label=spec["id"] + title_suffix)
    b = b if isinstance(b, dict) else {}
    run_id = h.get("caliban-run-id") or (b.get("caliban") or {}).get("run_id")
    choice = (b.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    rec = {"id": spec["id"], "status": status, "headers": {k: h.get(k) for k in framework.CALIBAN_HEADERS},
           "run_id": run_id}
    asked = choice.get("finish_reason") == "input_required" or (b.get("caliban") or {}).get("status") == "input_required"
    saw = model_saw(off, "[triage:assess]") if run_id else None
    if asked:
        rec["question"] = content
        ctx.client_panel(title, body, status, h, b, answer=content, rtl=rtl, model_saw=saw,
                         extra='<p class="muted">The node asks a question: the run waits for the customer app to '
                         'answer it.</p>')
        return rec, True
    if status == 504 and run_id:
        run = follow(key, run_id)
        ctx.run_timing(run_id, key, spec["id"])
        out = run.get("output")
        content = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    parsed = e2e_run.parse_answer(content) if run_id else None
    message = parsed.get("message") if isinstance(parsed, dict) and parsed.get("message") else content
    rec.update(answer=content, parsed=parsed)
    ctx.client_panel(title, body, status, h, b, answer=message, extra=answer_details(parsed), rtl=rtl, model_saw=saw)
    return rec, False


def answer_question(ctx, rec, answer):
    key = ST["api_key"]
    run_id = rec["run_id"]
    body = {"answer": answer}
    title = f"{rec['id']}: the customer app answers the node's question"
    ctx.client_panel(title, body, None, None, None, path=f"/v1/runs/{run_id}/input", pending="the run resumes…")
    ctx.pause(2)
    status, _, _ = ctx.gateway(f"/v1/runs/{run_id}/input", body, key, label=f"{rec['id']} answer")
    run = follow(key, run_id)
    ctx.run_timing(run_id, key, rec["id"])
    out = run.get("output")
    parsed = out if isinstance(out, dict) else e2e_run.parse_answer(out if isinstance(out, str) else "")
    message = (parsed or {}).get("message") or json.dumps(out, ensure_ascii=False)
    ctx.client_panel(title, body, status, {"run": run_id, "status": run["status"]}, None,
                     path=f"/v1/runs/{run_id}/input", answer=message, extra=answer_details(parsed))
    rec.update(answered=answer, final_status=run["status"], parsed=parsed)
    return rec


STAGE_LINE = {
    "R1": "English, with a name, a national ID number, a phone number and an e-mail address in the prompt.",
    "R2": "Not a triage request: it must not reach the node.",
    "R3": "French, a child with fever in Tripoli, with a name, a social security number and a phone number.",
    "R4": "Arabic, a skin rash in Beirut, with a name and a phone number.",
    "R5": "A case the node cannot place yet: it asks the patient where and since when.",
    "R6": "A heart check-up in Beirut. One catalogue row carries a prompt injection in its description.",
    "R7": "Severe chest pain in Tripoli: urgent, an emergency department, no question.",
}


def requests(ctx):
    """Requests through caliban/auto"""
    page = ctx.page
    results = []
    ctx.caption("Requests", "The marketplace app sends plain OpenAI chat completions to caliban/auto with its key. No "
                "Caliban-specific field: the gateway decides.", stage="Route", hold=3)
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7"):
        spec = REQUESTS[rid]
        ctx.caption(spec["id"], STAGE_LINE[rid], stage="Route")
        rec, asked = send_chat(ctx, rid)
        h = rec["headers"]
        if rid == "R2":
            ctx.caption(spec["id"], f"Intent {(h.get('x-caliban-intent') or 'none').split(';')[0]}: route "
                        f"{h.get('x-caliban-route')}, so a model answers"
                        + (f" ({h.get('x-caliban-route-fallback')})." if h.get("x-caliban-route-fallback") else "."),
                        stage="Route", hold=5)
            again, _ = send_chat(ctx, rid, " (again)")
            cache = again["headers"].get("x-caliban-cache")
            ctx.caption(spec["id"], "The same request again: answered from the tenant's exact cache, no model call "
                        "(x-caliban-cache: hit)." if cache == "hit" else f"The same request again: x-caliban-cache "
                        f"{cache}.", stage="Cache", hold=4)
            rec["repeat"] = again
            results.append(rec)
            continue
        ctx.caption(spec["id"], f"Classified {(h.get('x-caliban-intent') or 'none').split(';')[0]}, handed to "
                    f"{h.get('x-caliban-routed-model') or h.get('x-caliban-route')}: run {rec.get('run_id')}.",
                    stage="Route", hold=4)
        if asked:
            show_run(ctx, rec["run_id"], [("Nodes", "The run waits at the human step: the console shows the question, "
                                           "and the customer app answers it through the run API.", None)],
                     ended=False)
            rec = answer_question(ctx, rec, spec.get("answer") or spec.get("fallback_answer"))
            ctx.caption(spec["id"], "The run resumed: assess again with the answer, then the queries and the "
                        "recommendation.", stage="Nodes", hold=4)
        elif rid in ("R1", "R3", "R4"):
            ctx.caption(spec["id"], "Left, what the model received: the identifiers are replaced before any model call "
                        "(names need the NER tier, not deployed here).", stage="Screen", hold=5)
        if rid in ("R1", "R5", "R6") and rec.get("run_id"):
            lines = [("Ground", "Three catalogue queries (services, facilities, slots) as typed CQIR; the model only "
                      "filled enum values.", "steps")]
            if rid == "R1":
                lines += [("Screen", "Personal data protected in the run's model calls, counted here.", "pii"),
                          ("Log", "Billed as caliban/auto at the flat price, the real model cost metered next to it.",
                           "billed")]
            if rid == "R6":
                lines += [("Ground", "The hostile row reached the model only as data, labelled datasource; the answer "
                           "guard checked the answer against what the run read.", "guard")]
            show_run(ctx, rec["run_id"], lines, outputs=("services",) if rid in ("R1", "R6") else ())
        results.append(rec)
        with open(os.path.join(ctx.out_dir, f"{NAME}-requests.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    ctx.hide_panel()
    # The chapter ends on the tenant's runs, the last request's first: what the caption says is on screen.
    last = next((r.get("run_id") for r in reversed(results) if r.get("run_id")), None)
    ctx.goto(f"/runs?tenant={TENANT}")
    if last:
        expect(page.get_by_text(last, exact=True).first).to_be_visible()
    ctx.caption("Runs", f"Every node run of {TENANT}, newest first: status, version, who started it, cost and "
                "duration.", stage="Nodes", hold=4)


# ─────────────────────────── retrieval (E2E_RAG=1) ───────────────────────────


def retrieval(ctx):
    """Retrieval: documents, vetting, cited answers"""
    page = ctx.page
    m = e2e_rag.manifest()
    coll = e2e_rag.COLLECTION
    base = f"/api/v1/tenants/{TENANT}/rag/collections"
    ui_docs = os.environ.get("DEMO_RAG_UI_DOCS", "protocol-chest-pain-en,coverage-update-untrusted").split(",")
    ctx.goto(f"/rag?tenant={TENANT}")
    ctx.caption("Retrieval", "Ground on documents too: hospital protocols and the insurer's coverage policy, in English, "
                "French and Arabic.", stage="Ground", hold=3)
    if cp("GET", f"{base}/{coll}", ok=None).status != 200:
        ctx.click(page.get_by_role("button", name="New collection"))
        form = page.get_by_role("form", name="New collection")
        ctx.type(framework.label(form, "Name"), coll)
        ctx.type(framework.label(form, "Description"), m["collection"].get("description", ""))
        framework.label(form, "Embedding model").select_option(e2e_rag.EMBED_MODEL)
        if e2e_rag.RERANK_MODEL:
            framework.label(form, "Rerank model").select_option(e2e_rag.RERANK_MODEL)
        framework.label(form, "Default trust").select_option(m["collection"]["settings"]["default_trust"])
        ctx.caption("Collection", "Untrusted by default: any finding other than a link quarantines a document, and checks "
                    "must pass within the vetting deadline.", stage="Screen", hold=1.5)
        ctx.click(form.get_by_role("button", name="Create collection"))
        expect(form).to_be_hidden()
    docs_region = page.get_by_role("region", name=f"Documents of {coll}")
    if not docs_region.count():
        ctx.click(page.get_by_role("button", name=f"Show the documents of {coll}"))
    expect(docs_region).to_be_visible()
    for doc in m["documents"]:
        with open(os.path.join(common.HERE, "rag", doc["file"]), encoding="utf-8") as f:
            content = f.read()
        if doc["doc_id"] in ui_docs:
            ctx.click(docs_region.get_by_role("button", name="Ingest document"))
            form = page.get_by_role("form", name="Ingest document")
            expect(form).to_be_visible()
            box = framework.label(form, "Text")
            ctx.point(box)
            box.fill(content)
            ctx.type(framework.label(form, "Document id"), doc["doc_id"])
            framework.label(form, "Format").select_option("text/markdown")
            ctx.type(framework.label(form, "Title"), doc["title"])
            ctx.type(framework.label(form, "Source"), doc["source"])
            framework.label(form, "Trust").select_option(doc.get("trust") or "")
            if doc.get("poisoned"):
                ctx.caption("Ingest", "An untrusted upload on the same topic as a question: it carries a prompt "
                            "injection and an exfiltration request.", stage="Screen", hold=2)
            ctx.click(form.get_by_role("button", name="Ingest document"))
            expect(form).to_be_hidden()
        else:
            body = {"doc_id": doc["doc_id"], "title": doc["title"], "content": content, "content_type": "text/markdown",
                    "source": doc["source"]}
            body.update({k: doc[k] for k in ("trust", "valid_from", "valid_to") if doc.get(k)})
            cp("POST", f"{base}/{coll}/documents", body)
    rest = len([x for x in m["documents"] if x["doc_id"] not in ui_docs])
    ctx.api_caption("Ingest", f"The other {rest} documents, among them last year's policy whose validity window has "
                    "ended, ingested the same way.", "POST .../rag/collections/{c}/documents for the rest (the console "
                    "ingests one document at a time)", stage="Ground")
    ctx.caption("Indexing", "The documents are pseudonymised, embedded through the gateway and indexed; the injection "
                "scan runs on each.", stage="Ground", clock=True)
    with ctx.idle("indexing"):
        docs = wait_until("the documents to be indexed", lambda: (lambda vs: vs if all(
            (v.get("versions") or [{}])[0].get("status") in e2e_rag.TERMINAL_DOC for v in vs) else None)(
            [e2e_rag.doc_versions(x["doc_id"]) for x in m["documents"]]),
            timeout=int(os.environ.get("E2E_RAG_INDEX_TIMEOUT_S", "600")), every=2)
    page.reload()
    expect(docs_region).to_be_visible()
    statuses = {x["doc_id"]: (v.get("versions") or [{}])[0].get("status") for x, v in zip(m["documents"], docs)}
    poisoned = [x["doc_id"] for x in m["documents"] if x.get("poisoned")]
    ctx.point(docs_region.get_by_role("table"), 1)
    ctx.caption("Vetting", f"{sum(1 for s in statuses.values() if s == 'ready')} documents ready; "
                + ", ".join(f"{p} {statuses.get(p)}" for p in poisoned) + ": it is never served.", stage="Screen", hold=4)
    for p in poisoned:
        btn = docs_region.get_by_role("button", name=f"Review {p}")
        if btn.count():
            ctx.click(btn)
            ctx.pause(4)
            page.keyboard.press("Escape")

    with open(os.path.join(common.HERE, "node", f"{e2e_rag.NODE}.node.json"), encoding="utf-8") as f:
        spec = json.loads(f.read().replace("__CHAT_MODEL__", CHAT_ID).replace("__RAG_COLLECTION__", coll))
    nbase = f"/api/v1/tenants/{TENANT}/nodes/{e2e_rag.NODE}"
    listed = cp("GET", f"{nbase}/versions", ok=None)
    existing = listed.body.get("versions", []) if listed.status == 200 else []
    if not [v for v in existing if v["spec"] == spec and v["state"] != "retired"]:
        ctx.nav("Nodes")
        ctx.click(page.get_by_role("button", name="New node").first)
        form = page.get_by_role("form", name="Create node")
        framework.label(form, "Tenant").select_option(TENANT)
        ctx.type(framework.label(form, "Name"), e2e_rag.NODE)
        form.locator("#node-spec").fill(json.dumps(spec, indent=2, ensure_ascii=False))
        expect(form.get_by_text("Valid against node.schema.json")).to_be_visible()
        ctx.caption("careguide", f"The careguide agent: builtin://rag_search under the scope rag.{coll}:read, answers "
                    "with cited passages.", stage="Nodes", hold=2)
        ctx.click(form.get_by_role("button", name="Create node"))
        expect(form).to_be_hidden()
    else:
        ctx.goto(f"/nodes/{TENANT}/{e2e_rag.NODE}")
    expect(page.get_by_role("table", name="Versions")).to_be_visible()
    v = [x for x in cp("GET", f"{nbase}/versions").body["versions"] if x["spec"] == spec and x["state"] != "retired"][-1]
    ver = v["version"]
    if v["state"] == "draft":
        ctx.click(page.get_by_role("button", name=f"Publish v{ver}"))
        dlg = ctx.dialog(f"Publish {e2e_rag.NODE} v{ver}?")
        box = framework.label(dlg, "Make it the live version")
        if box.is_checked():
            ctx.click(box)
        ctx.click(dlg.get_by_role("button", name="Publish", exact=True))
        expect(dlg).to_be_hidden()
    if not v.get("live"):
        # A version with read tools goes live only after a finished injection eval (the gate).
        ctx.goto(f"/nodes/{TENANT}/{e2e_rag.NODE}?tab=evals&ev={ver}")
        expect(page.get_by_role("heading", name=f"Evals of v{ver}")).to_be_visible()
        ctx.caption("careguide", "It reads documents, so the gate wants an injection eval before it goes live: the "
                    "attacks ride on a coverage question.", stage="Nodes", hold=2)
        ctx.click(page.get_by_role("button", name="Start eval").first)
        dlg = ctx.dialog(f"Start an eval of {e2e_rag.NODE} v{ver}")
        framework.label(dlg, "Suite").select_option("injection")
        framework.label(dlg, "Run input").select_option("__custom")
        box = framework.label(dlg, "Custom input")
        ctx.point(box)
        box.fill(json.dumps(m["requests"][0]["text"], ensure_ascii=False))
        before = {e["id"] for e in (cp("GET", f"/api/v1/tenants/{TENANT}/evals").body.get("data") or [])}
        ctx.click(dlg.get_by_role("button", name="Start eval"))
        expect(dlg).to_be_hidden()
        new = wait_until("the eval to be listed", lambda: [e for e in (cp(
            "GET", f"/api/v1/tenants/{TENANT}/evals").body.get("data") or []) if e["id"] not in before], timeout=30)
        ctx.caption("careguide", "The injection suite is running.", stage="Nodes", clock=True)
        with ctx.idle("careguide injection eval"):
            ev = wait_until("the careguide injection eval", lambda: (lambda b: b if b["status"] in (
                "succeeded", "failed", "cancelled") else None)(
                cp("GET", f"/api/v1/tenants/{TENANT}/evals/{new[0]['id']}").body),
                timeout=int(os.environ.get("E2E_EVAL_TIMEOUT_S", "1800")), every=2)
        ctx.record_eval(f"careguide v{ver} injection", ev)
        ST["rag_injection_eval"] = {"id": ev["id"], "status": ev["status"], "summary": ev.get("summary")}
        page.keyboard.press("Escape")
        page.reload()
        badge = page.get_by_text(re.compile(r"^\s*(Meets the bar|Blocked by the promotion bar)\s*$"))
        expect(badge).to_be_visible()
        ctx.point(badge, 1)
        s = ev.get("summary") or {}
        ctx.caption("careguide", f"Injection eval {ev['status']}: {s.get('blocked')} of {s.get('trials')} attacks "
                    "blocked.", stage="Nodes", hold=3)
        ctx.click(page.get_by_role("button", name=f"Make v{ver} live").first)
        dlg = ctx.dialog(f"Make {e2e_rag.NODE} v{ver} live?")
        ctx.click(dlg.get_by_role("button", name="Make live"))
        expect(dlg).to_be_hidden()
        expect(page.get_by_text(f"Live: v{ver}")).to_be_visible()
    v = [x for x in cp("GET", f"{nbase}/versions").body["versions"] if x["version"] == v["version"]][0]
    rag = ST.get("rag") or {}
    if not rag.get("api_key"):
        key, kid = mint_key(ctx, "medmarket-careguide", [e2e_rag.NODE], scopes=[f"rag.{coll}:read"], caption=(
            "API key", f"A key that may run careguide only, narrowed to rag.{coll}:read.", "Clear"))
        rag.update(api_key=key, api_key_id=kid)
    c = cp("GET", f"{base}/{coll}").body
    rag.update(collection=coll, embedding_model=c.get("embedding_model"), rerank_model=c.get("rerank_model"),
               node_version=v["version"], node_hash=v["hash"],
               documents={x["doc_id"]: {"status": statuses.get(x["doc_id"])} for x in m["documents"]})
    ST["rag"] = rag
    save_state(ST)

    answers = []
    for req in m["requests"]:
        body = {"input": req["text"]}
        path = f"/v1/nodes/{e2e_rag.NODE}/runs"
        title = f"{req['id']}: {req.get('title', '')}"
        ctx.client_panel(title, body, None, None, None, path=path, pending="running careguide…")
        ctx.pause(2)
        status, h, b = ctx.gateway(path, body, rag["api_key"], label=req["id"].split("-")[0])
        run_id = b.get("id") if isinstance(b, dict) else None
        run = follow(rag["api_key"], run_id) if run_id else {}
        if run_id:
            ctx.run_timing(run_id, rag["api_key"], req["id"].split("-")[0])
        parsed = e2e_rag.parse_json(run.get("output")) or {}
        text = parsed.get("answer") or json.dumps(run.get("output"), ensure_ascii=False)
        cites = parsed.get("citations") or []
        extra = ("<h4>Citations</h4><table>" + "".join(
            f'<tr><td class="k">[{esc(x.get("n"))}]</td><td>{esc(x.get("doc_id"))}</td></tr>' for x in cites[:6])
                 + "</table>") if cites else ""
        ctx.client_panel(title, body, status, dict(h, run=run_id, status=run.get("status")), None, path=path,
                         answer=str(text), extra=extra, rtl=req.get("language") == "ar")
        ctx.caption(req["id"].split("-")[0], f"Answered with {len(cites)} citations of passages the search returned; the "
                    "search result carries the retrieved taint label.", stage="Ground", hold=5)
        answers.append({"id": req["id"], "run_id": run_id, "status": run.get("status"), "answer": text,
                        "citations": cites})
    ctx.hide_panel()
    if answers and answers[0].get("run_id"):
        show_run(ctx, answers[0]["run_id"], [("Ground", "The rag_search step and the model call after it carry the "
                                              "retrieved label.", "steps")])
    with open(os.path.join(ctx.out_dir, f"{NAME}-rag-requests.json"), "w", encoding="utf-8") as f:
        json.dump(answers, f, indent=2, ensure_ascii=False)


def close(ctx):
    """Usage and audit"""
    page = ctx.page
    ctx.goto(f"/usage?tenant={TENANT}")
    expect(page.get_by_role("heading", name="Usage", exact=True)).to_be_visible()
    ctx.caption("Usage", "One usage event per model call: tokens, cache outcome, personal data counted, the real cost "
                "and what caliban/auto billed. No prompt or answer is kept.", stage="Log", hold=4)
    page.mouse.wheel(0, 700)
    ctx.pause(3)
    page.mouse.wheel(0, 900)
    ctx.pause(3)
    ctx.goto(f"/audit?tenant={TENANT}")
    chain = framework.label(page, "Chain integrity")
    expect(chain.first).to_be_visible()
    ctx.point(chain.first, 1)
    ctx.caption("Audit", "Every change made in this walkthrough is in the hash-chained audit log, and the console "
                "verifies the chain.", stage="Log", hold=5)
