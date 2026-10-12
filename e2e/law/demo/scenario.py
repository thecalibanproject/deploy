"""Law firm: a sovereign legal assistant for Hadad & Associés (Beirut and Paris).

The scenario of the recorded demo (e2e/demo/README.md is the contract). An operator sets up the
firm's tenant in the console: privacy settings for client data, the practice database (MongoDB)
and its reviewed ontology, a collection of legal texts with a poisoned upload that vetting
quarantines, three nodes (matter-desk, research and the legal-assistant orchestrator that pins
them), their evals and the promotion gate, the routing intent and an API key. Then the firm's
intranet sends seven requests, in French and English, to caliban/auto, and the console shows what
happened to each: the subnode tree, the typed queries, the protected personal data, the
citations, the answer guard and the cost. Usage and the audit log close it.

Every value comes from the files next to this one (lib/law.py renders the node specs, the CSM,
the documents, the requests), and every caption is built from what the API answered. Where a
console step does not complete, the step goes through the admin API and the caption says so.
State for check.py: $E2E_WORK_DIR/law/state.json.
"""
import html
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import law  # noqa: E402
from law import TENANT  # noqa: E402

try:  # e2e/demo is on sys.path when the recorder loads this file
    from framework import ApiError
    from framework import label as lab
except ImportError:  # pragma: no cover (compile checks outside the recorder)
    class ApiError(Exception):
        status = None
        body = None

    def lab(scope, text):
        """The control labelled exactly `text` (framework.label)."""
        return scope.get_by_label(re.compile("^" + re.escape(text) + r"\s*\*?$"))

NAME = "law"
TITLE = "A legal assistant for a law firm (Beirut and Paris)"
TERMINAL_RUN = {"succeeded", "failed", "budget_exhausted", "cancelled"}
TERMINAL_DOC = {"ready", "quarantined", "failed"}
NODE_BASE = f"/api/v1/tenants/{TENANT}/nodes"
RAG_BASE = f"/api/v1/tenants/{TENANT}/rag/collections"


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def chapters(ctx):
    return [("intro", intro), ("model", model), ("tenant", tenant), ("datasource", datasource),
            ("ontology", ontology), ("retrieval", retrieval), ("subnodes", subnodes),
            ("orchestrator", orchestrator), ("intents", intents), ("apikey", apikey),
            ("requests", requests), ("close", close)]


# ─────────────────────────── helpers ───────────────────────────


def st_load():
    return law.load_state()


def st_save(st):
    law.save_state(st)


def api_try(ctx, method, path, body=None):
    """(status, body) without raising."""
    try:
        return 200, getattr(ctx.api, method)(path, body)
    except Exception as e:  # noqa: BLE001 (framework.ApiError: framework.py may run as __main__, so match by shape)
        if not hasattr(e, "status"):
            raise
        return e.status, getattr(e, "body", None)


def wait(what, fn, timeout=120, every=2.0):
    return law.wait_until(what, fn, timeout=timeout, every=every)


def goto(ctx, route):
    """A console route; a page that does not load is reported, not fatal (the step after it says
    what it could not show)."""
    try:
        ctx.goto(route)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"   console route {route} did not load ({type(e).__name__}: {str(e)[:160]})", flush=True)
        return False


def pick_tenant(ctx):
    """The page's tenant picker (Ontology, Retrieval), set to the firm's tenant."""
    for loc in (lambda: lab(ctx.page, "Tenant"), lambda: ctx.page.get_by_role("combobox").first):
        try:
            sel = loc()
            sel.first.select_option(TENANT, timeout=5000)
            ctx.pause(1)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def nav(ctx, label, route):
    """The sidebar entry, else its route."""
    try:
        ctx.nav(label)
    except Exception:  # noqa: BLE001
        goto(ctx, route)


def api_note(ctx, title, line, what, stage=None, hold=4):
    """A step done through the admin API on purpose (the console has no form for that exact
    step): listed in the timeline's api_steps, and said on screen."""
    ctx.timeline["api_steps"].append({"chapter": ctx._chapter, "what": what})
    ctx.caption(title, line, stage=stage, note=f"Admin API: {what}", hold=hold)


def console_step(ctx, what, ui, fallback, title="Console", stage=None, verify=None):
    """Runs a console interaction; if it does not complete, does the same through the admin API
    and says so on screen (and in the timeline), so the recording never claims a click that did
    not happen."""
    try:
        ui()
        if verify is not None:
            try:
                law.wait_until(f"{what} to show in the API", verify, timeout=8, every=0.5)
            except TimeoutError:
                raise RuntimeError("the console step left no trace in the API") from None
        return True
    except Exception as e:  # noqa: BLE001 (the fallback keeps the scenario going, and is reported)
        print(f"   console step '{what}' did not complete ({type(e).__name__}: {str(e)[:200]}); admin API instead",
              flush=True)
        ctx.screenshot(f"fallback-{re.sub(r'[^a-z0-9]+', '-', what.lower())[:40]}")
        try:
            ctx.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        try:
            fallback()
        except Exception as e2:  # noqa: BLE001 (framework.ApiError, matched by shape)
            if getattr(e2, "status", None) != 409:  # 409: the console got as far as creating it
                raise
        ctx.timeline["api_steps"].append({"chapter": ctx._chapter, "what": f"{what} (console fallback)"})
        ctx.caption(title, f"{what}: done through the admin API.", stage=stage,
                    note="The console step did not complete in this recording; the same call went through the admin API.",
                    hold=3)
        return False


def table_html(rows, head=None):
    h = "".join(f"<td>{esc(x)}</td>" for x in head) if head else ""
    body = "".join("<tr>" + "".join(f'<td{" class=k" if i == 0 else ""}>{esc(c)}</td>' for i, c in enumerate(r))
                   + "</tr>" for r in rows)
    return f"<table>{('<tr>' + h + '</tr>') if h else ''}{body}</table>"


def card_html(title, sub, inner):
    return (f'<div class="card"><div class="head"><span class="t">{esc(title)}</span><span class="s">{esc(sub)}</span>'
            f'</div><div class="cols"><div class="col" style="flex:1">{inner}</div></div></div>')


# ─────────────────────────── chapters ───────────────────────────


def intro(ctx):
    """Hadad & Associés: the firm and the request path"""
    ctx.card('<div><div class="big">A law firm, Beirut and Paris</div>'
             '<div class="sub">Hadad &amp; Associés (fictional) gives its lawyers one assistant for matter questions, '
             'conflict checks and legal research. Client data stays on the firm\'s hardware: Caliban screens it, '
             'grounds every answer on the practice database or on cited texts, checks the answer and logs every '
             'call.</div><div class="stages"><span>Clear</span><span>Route</span><span>Screen</span><span>Cache</span>'
             '<span>Ground</span><span>Land</span><span>Log</span></div>'
             f'<div class="foot">Tenant {esc(TENANT)} · synthetic data, invented names and identifiers · model '
             f'{esc(law.UPSTREAM)}</div></div>')
    ctx.pause(6)
    ctx.hide_card()


def model(ctx):
    """The firm's chat model"""
    nav(ctx, "Models", "/models")
    ctx.caption("Models", f"The firm's chat model, {law.CHAT_ID}: {law.UPSTREAM} on the firm's own server, trust tier "
                f"{law.TRUST_TIER.replace('_', ' ')}.", stage="Land")
    providers = {p["id"] for p in (ctx.api.get("/api/v1/providers") or [])}
    models = {m["id"] for m in (ctx.api.get("/api/v1/models") or [])}
    caps = {"tools": True}
    if law.FAMILY.startswith("qwen3"):
        caps.update({"reasoning": "hybrid", "reasoning_control": "enable_thinking"})
    provider = {"id": law.PROVIDER, "kind": "openai_compatible", "base_url": law.CHAT_BASE_URL,
                "trust_tier": law.TRUST_TIER, "cache_salt": law.TESTBED}
    entry = {"id": law.CHAT_ID, "provider": law.PROVIDER, "upstream_model": law.UPSTREAM, "kind": "chat",
             "family": law.FAMILY or None, "trust_tier": law.TRUST_TIER, "capabilities": caps, "context_window": 32768,
             "licence": "apache-2.0", "price_in_per_mtok": law.PRICE_IN, "price_out_per_mtok": law.PRICE_OUT}
    page = ctx.page
    if law.PROVIDER not in providers:
        def ui():
            ctx.click(page.get_by_role("tab", name="Model servers"))
            ctx.click(page.get_by_role("button", name="Add server"))
            dlg = ctx.dialog("Add model server")
            ctx.type(lab(dlg, "Server ID"), law.PROVIDER)
            lab(dlg, "Kind").select_option("openai_compatible")
            ctx.type(lab(dlg, "Base URL"), law.CHAT_BASE_URL)
            lab(dlg, "Trust tier").select_option(law.TRUST_TIER)
            if law.TESTBED:
                ctx.click(lab(dlg, "Cache salt"))
            ctx.click(dlg.get_by_role("button", name="Add server"))
            dlg.wait_for(state="hidden")
        console_step(ctx, f"model server {law.PROVIDER}", ui, lambda: ctx.api.post("/api/v1/providers", provider),
                     "Models", "Land",
                     verify=lambda: any(p["id"] == law.PROVIDER for p in ctx.api.get("/api/v1/providers") or []))
    if law.CHAT_ID not in models:
        def ui():
            ctx.click(page.get_by_role("tab", name="Catalogue"))
            ctx.click(page.get_by_role("button", name="Add model"))
            dlg = ctx.dialog("Add model")
            ctx.type(lab(dlg, "Model ID"), law.CHAT_ID)
            ctx.type(lab(dlg, "Provider"), law.PROVIDER)
            ctx.type(lab(dlg, "Upstream model"), law.UPSTREAM)
            lab(dlg, "Kind").select_option("chat")
            if law.FAMILY:
                ctx.type(lab(dlg, "Family"), law.FAMILY)
            lab(dlg, "Trust tier").select_option(law.TRUST_TIER)
            ctx.type(lab(dlg, "Licence"), "apache-2.0")
            ctx.type(lab(dlg, "Context window"), "32768")
            ctx.type(lab(dlg, "Price in (USD / Mtok)"), f"{law.PRICE_IN:g}")
            ctx.type(lab(dlg, "Price out (USD / Mtok)"), f"{law.PRICE_OUT:g}")
            tools = dlg.get_by_label("Tools", exact=True)
            if not tools.is_checked():
                ctx.click(tools)
            if law.FAMILY.startswith("qwen3"):
                dlg.get_by_label("Reasoning", exact=True).select_option("hybrid")
                lab(dlg, "Reasoning control").select_option("enable_thinking")
            ctx.click(dlg.get_by_role("button", name="Add model"))
            dlg.wait_for(state="hidden")
        console_step(ctx, f"catalogue entry {law.CHAT_ID}", ui, lambda: ctx.api.post("/api/v1/models", entry),
                     "Models", "Land",
                     verify=lambda: any(m["id"] == law.CHAT_ID for m in ctx.api.get("/api/v1/models") or []))
    m = next((x for x in ctx.api.get("/api/v1/models") or [] if x.get("id") == law.CHAT_ID), {})
    ctx.caption("Models", f"{law.CHAT_ID}: {m.get('upstream_model')} ({m.get('trust_tier')}), hybrid reasoning switched "
                "per call: every vertex of the firm's nodes runs with thinking off and constrained JSON output."
                if law.FAMILY.startswith("qwen3") else
                f"{law.CHAT_ID}: {m.get('upstream_model')} ({m.get('trust_tier')}), priced for metering.",
                stage="Land", hold=3)


def tenant(ctx):
    """Tenant cabinet-hadad: privacy for client data"""
    pii = law.load("config/pii.json")
    st = st_load()
    page = ctx.page
    nav(ctx, "Tenants", "/tenants")
    ctx.caption("Tenant", f"Create the tenant {TENANT}: the firm's isolation boundary. Keys, caches, ontology, documents "
                "and budgets never cross it.", stage="Clear")
    status, t = api_try(ctx, "get", f"/api/v1/tenants/{TENANT}")
    if status != 200:
        def ui():
            ctx.click(page.get_by_role("button", name="New tenant").first)
            dlg = ctx.dialog("New tenant")
            ctx.type(lab(dlg, "Name"), TENANT)
            lab(dlg, "Default PII handling").select_option("reversible")
            ctx.click(dlg.get_by_role("button", name="Create tenant"))
            dlg.wait_for(state="hidden")
        console_step(ctx, f"tenant {TENANT}", ui,
                     lambda: ctx.api.post("/api/v1/tenants", {"name": TENANT, "pii_default": "reversible"}), "Tenant", "Clear",
                     verify=lambda: api_try(ctx, "get", f"/api/v1/tenants/{TENANT}")[0] == 200)

    def privacy_ui():
        ctx.click(page.get_by_role("link", name=f"Open {TENANT}"))
        form = page.get_by_role("form", name="Privacy and caching settings")
        form.scroll_into_view_if_needed()
        mode = lab(form, "PII default mode")
        if mode.input_value() != "reversible":
            mode.select_option("reversible")
        ctx.point(mode, 1.0)
        sw = lab(form, "PII mode on sovereign models")
        if sw.get_attribute("aria-checked") != "true":
            ctx.click(sw)
        ctx.point(sw, 1.0)
        save = form.get_by_role("button", name="Save")
        if save.is_enabled():
            ctx.click(save)
    console_step(ctx, "privacy settings", privacy_ui,
                 lambda: ctx.api.patch(f"/api/v1/tenants/{TENANT}", {"pii_default": "reversible", "pii_on_sovereign": True}),
                 "Privacy", "Screen", verify=lambda: (lambda t: t.get("pii_default") == "reversible"
                                                      and t.get("pii_on_sovereign") is True)(
                     ctx.api.get(f"/api/v1/tenants/{TENANT}")))
    ctx.caption("Privacy", "PII mode reversible, on sovereign models too: before any model call, names, e-mail addresses, "
                "phone numbers and IDs are swapped for consistent surrogates, and the answer is restored for the lawyer. "
                "Professional secrecy holds even on the firm's own GPU.", stage="Screen", hold=5)

    pat = pii["pii_patterns"][0]
    t = ctx.api.get(f"/api/v1/tenants/{TENANT}")
    if not any(p.get("pattern") == pat["pattern"] for p in t.get("pii_patterns") or []):
        def det_ui():
            det = page.get_by_role("form", name="Personal data detectors")
            det.scroll_into_view_if_needed()
            ctx.click(det.get_by_role("button", name="Add pattern"))
            n = det.get_by_label(re.compile(r"^Label of pattern \d+$")).count()
            ctx.type(lab(det, f"Label of pattern {n}"), pat["label"])
            ctx.type(det.get_by_label(f"Pattern {n}", exact=True), pat["pattern"])
            ctx.click(det.get_by_role("button", name="Save"))
        console_step(ctx, "the national ID pattern", det_ui,
                     lambda: ctx.api.patch(f"/api/v1/tenants/{TENANT}", {"pii_patterns": pii["pii_patterns"]}),
                     "Detectors", "Screen", verify=lambda: any(p.get("pattern") == pat["pattern"] for p in
                                                               ctx.api.get(f"/api/v1/tenants/{TENANT}").get("pii_patterns") or []))
    ctx.caption("Detectors", "A tenant pattern for the Lebanese national ID number of the firm's records (LB-dddd-dddd-d): "
                "no built-in detector knows it. The French NIR, e-mail addresses and phone numbers are built in.",
                stage="Screen", hold=4)
    ctx.api.patch(f"/api/v1/tenants/{TENANT}", {"pii_dictionary": pii["pii_dictionary"]})
    api_note(ctx, "Detectors", f"The tenant's PII dictionary: {len(pii['pii_dictionary'])} first and family names of the "
             "people in the practice database, one term each, so a family name alone and a full name get the same "
             "surrogate, without the NER tier.", "PATCH /api/v1/tenants/{t} pii_dictionary (terms are never shown)",
             "Screen", hold=5)
    caps = {"daily_usd": 20, "monthly_usd": 200}
    ctx.api.patch(f"/api/v1/tenants/{TENANT}", {"node_spend_caps": caps})
    ctx.api.put(f"/api/v1/tenants/{TENANT}/routes", {"routes": [{"intent": "default", "models": [law.CHAT_ID]}]})
    api_note(ctx, "Limits and routes", f"Node spend caps of {caps['daily_usd']} USD a day and {caps['monthly_usd']} a month, "
             f"checked before every model call; the default route sends requests no intent claims to {law.CHAT_ID}.",
             "PATCH /api/v1/tenants/{t} node_spend_caps, PUT /api/v1/tenants/{t}/routes", "Clear", hold=4)
    t = ctx.api.get(f"/api/v1/tenants/{TENANT}")
    st["tenant"] = {k: t.get(k) for k in ("pii_default", "pii_on_sovereign", "pii_patterns", "pii_dictionary_count",
                                          "node_spend_caps")}
    st["started_at"] = st.get("started_at") or time.time()
    st_save(st)


def datasource(ctx):
    """The practice database: MongoDB, read-only, introspected"""
    st = st_load()
    page = ctx.page
    nav(ctx, "Datasources", "/datasources")
    ctx.caption("Datasource", "Connect the firm's practice database (MongoDB: clients, matters, time entries) with a "
                "read-only principal. Caliban refuses a user that can write; the password is sealed and never shown "
                "again.", stage="Ground")
    live = [d for d in ctx.api.get(f"/api/v1/datasources?tenant_id={TENANT}") or [] if d["name"] == law.DATASOURCE]
    if not live:
        uri = law.mongo_uri()
        body = {"tenant_id": TENANT, "kind": "mongodb", "name": law.DATASOURCE,
                "connection": {"uri": uri, "database": law.DATABASE}}
        rp = os.environ.get("E2E_LAW_READ_PREFERENCE", "")
        if rp:
            body["connection"]["read_preference"] = rp

        def ui():
            ctx.click(page.get_by_role("button", name="Register datasource").first)
            form = page.get_by_role("form", name="Register datasource")
            form.wait_for()
            lab(form, "Tenant").select_option(TENANT)
            lab(form, "Kind").select_option("mongodb")
            ctx.type(lab(form, "Name"), law.DATASOURCE)
            field = lab(form, "Connection URI")
            ctx.point(field)
            field.fill(uri)  # a password field: the video shows dots only
            ctx.type(lab(form, "Database"), law.DATABASE)
            lab(form, "Read preference").select_option(rp or "secondary")
            ctx.click(form.get_by_role("button", name="Register datasource"))
            form.wait_for(state="hidden")
        console_step(ctx, f"datasource {law.DATASOURCE}", ui, lambda: ctx.api.post("/api/v1/datasources", body),
                     "Datasource", "Ground", verify=lambda: any(d["name"] == law.DATASOURCE for d in ctx.api.get(
                         f"/api/v1/datasources?tenant_id={TENANT}") or []))
        live = [d for d in ctx.api.get(f"/api/v1/datasources?tenant_id={TENANT}") or [] if d["name"] == law.DATASOURCE]
    ds = live[0]["id"]
    st["datasource_id"] = ds
    ctx.caption("Introspection", "Introspect: the control plane samples each collection through the reader and proposes "
                "the entities, embedded arrays and references it infers, for a person to review.", stage="Ground")

    def ui():
        row = page.get_by_role("table", name="Datasources").get_by_role("row").filter(has_text=law.DATASOURCE)
        ctx.click(row.get_by_role("button", name=f"Introspect {law.DATASOURCE}"))
    console_step(ctx, "introspection", ui, lambda: api_try(ctx, "post", f"/api/v1/datasources/{ds}/introspect"),
                 "Introspection", "Ground")
    with ctx.idle("introspection"):
        try:
            job = wait("the introspection job", lambda: (lambda j: j if j and j.get("status") != "running" else None)(
                ctx.api.get(f"/api/v1/datasources/{ds}/introspection")), timeout=300)
        except TimeoutError:
            job = {"status": "timeout"}
    st["introspection"] = {k: job.get(k) for k in ("id", "status", "objects", "proposed", "skipped", "version", "error")}
    st_save(st)
    if job.get("status") == "succeeded":
        ctx.caption("Introspection", f"Introspection succeeded: {job.get('objects')} collections, {job.get('proposed')} "
                    f"elements proposed, ontology version {job.get('version')}.", stage="Ground", hold=4)
    else:
        ctx.caption("Introspection", f"Introspection ended {job.get('status')}"
                    + (f": {job.get('error')}" if job.get("error") else "") + ". The curated model alone is enough.",
                    stage="Ground", hold=4)


def ontology(ctx):
    """Ontology review: the firm's semantic model"""
    st = st_load()
    page = ctx.page
    csm = law.load("ontology/law.csm.json")
    curated = {e["id"]: e for e in csm["elements"]}
    base = f"/api/v1/tenants/{TENANT}/ontology"
    nav(ctx, "Ontology", "/ontology")
    pick_tenant(ctx)
    kinds = {}
    for e in curated.values():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    ctx.caption("Ontology", f"The firm's semantic model: {kinds.get('entity')} entities (clients, matters with their "
                f"deadlines and parties, time entries), {kinds.get('attribute')} attributes, {kinds.get('metric')} metrics, "
                f"{kinds.get('relation')} references, a policy and glossary terms. Node queries name these ids only.",
                stage="Ground", hold=4)
    onto = ctx.api.get(base)
    current = {e["id"]: e for e in onto["elements"]}
    fresh = [e for i, e in curated.items()
             if not (current.get(i, {}).get("status") == "approved" and current[i].get("kind") == e["kind"]
                     and law.covers(current[i].get("spec"), e["spec"]))]
    if fresh:
        def ui():
            ctx.click(page.get_by_role("button", name="Propose elements"))
            dlg = ctx.dialog("Propose elements")
            box = lab(dlg, "Elements (JSON)")
            ctx.point(box)
            box.fill(json.dumps(fresh, indent=2, ensure_ascii=False))
            ctx.pause(1.5)
            ctx.click(dlg.get_by_role("button", name="Propose", exact=True))
            dlg.get_by_role("status").filter(has_text=re.compile(r"^(Wrote \d+ proposals?|Nothing written)")).wait_for()
            ctx.pause(2)
            ctx.click(dlg.get_by_role("button", name="Close", exact=True).last)
        console_step(ctx, f"propose {len(fresh)} curated elements", ui,
                     lambda: ctx.api.post(f"{base}/proposals", {"elements": fresh}), "Ontology", "Ground")
    onto = ctx.api.get(base)
    pending = [e for e in onto["elements"] if e["status"] == "proposed"]
    names = {}
    for e in pending:
        names[e["name"]] = names.get(e["name"], 0) + 1
    show = [e for e in pending if e["id"] in ("policy.client_identifiers", "rel.matter_client") and names[e["name"]] == 1]
    queue = page.get_by_role("table", name="Review queue")
    for e in show:
        def ui(e=e):
            btn = queue.get_by_role("button", name=f"Approve {e['name']}", exact=True)
            ctx.click(btn)
            btn.wait_for(state="hidden")
        if e["id"].startswith("policy"):
            ctx.caption("Review", "The policy: the clients' national identifiers (Lebanese ID, French NIR) are denied to "
                        "every query; the compiler refuses a query that names them.", stage="Ground", hold=2)
        else:
            ctx.caption("Review", "A reference: each matter belongs to one client (matters.client_id to clients._id), "
                        "verified, so a query may join along it.", stage="Ground", hold=2)
        console_step(ctx, f"approve {e['name']}", ui, lambda e=e: ctx.api.post(
            f"{base}/review", {"decisions": [{"id": e["id"], "decision": "approve"}], "note": "demo review"}),
            "Review", "Ground")
    onto = ctx.api.get(base)
    before = onto["version"]
    pending = [e for e in onto["elements"] if e["status"] == "proposed"]
    decisions = [{"id": e["id"], "decision": "approve" if e["id"] in curated else "reject"} for e in pending]
    if decisions:
        r = ctx.api.post(f"{base}/review", {"decisions": decisions,
                                            "note": "demo: reviewed against the firm's practice database"})
        n_ok = sum(1 for d in decisions if d["decision"] == "approve")
        st["review_batch"] = {"decisions": len(decisions), "from": before, "to": r["version"]}
        api_note(ctx, "Review", f"The other {len(decisions)} decisions ({n_ok} approvals, {len(decisions) - n_ok} rejections "
                 f"of introspection's own names for the same paths) in one review batch: version {before} to "
                 f"{r['version']}.", "POST /api/v1/tenants/{t}/ontology/review (the console reviews one element at a time)",
                 "Ground")
        try:
            page.reload()
        except Exception:  # noqa: BLE001
            pass
    onto = ctx.api.get(base)
    approved = {e["id"] for e in onto["elements"] if e["status"] == "approved"}
    missing = sorted(set(curated) - approved)
    if missing:
        raise RuntimeError(f"curated elements not approved: {missing}")
    st["ontology_version"] = f"{TENANT}@{onto['version']}"
    st_save(st)
    ctx.caption("Ontology", f"{len(approved)} approved elements, version {st['ontology_version']}. The model picks, the "
                "compiler writes: nodes send typed CQIR, a Rust compiler lowers it to a MongoDB pipeline.",
                stage="Ground", hold=4)


def retrieval(ctx):
    """Legal texts: collection, ingest, vetting"""
    st = st_load()
    page = ctx.page
    m = law.load("rag/documents.json")
    coll = law.COLLECTION
    nav(ctx, "Retrieval", "/rag")
    pick_tenant(ctx)
    ctx.caption("Retrieval", "Ground answers on texts too: demo summaries of the French Code civil and the Lebanese Code "
                "of Obligations and Contracts, the firm's memos, policy and templates.", stage="Ground")
    status, c = api_try(ctx, "get", f"{RAG_BASE}/{coll}")
    body = {"name": coll, "embedding_model": law.EMBED_MODEL, **m["collection"]}
    if law.RERANK_MODEL:
        body["rerank_model"] = law.RERANK_MODEL
    if status != 200:
        def ui():
            ctx.click(page.get_by_role("button", name="New collection"))
            form = page.get_by_role("form", name="New collection")
            ctx.type(lab(form, "Name"), coll)
            ctx.type(lab(form, "Description"), m["collection"]["description"])
            lab(form, "Embedding model").select_option(law.EMBED_MODEL)
            if law.RERANK_MODEL:
                lab(form, "Rerank model").select_option(law.RERANK_MODEL)
            lab(form, "Default trust").select_option("untrusted")
            ctx.click(form.get_by_role("button", name="Create collection"))
            form.wait_for(state="hidden")
        console_step(ctx, f"collection {coll}", ui, lambda: ctx.api.post(RAG_BASE, body), "Retrieval", "Ground",
                     verify=lambda: api_try(ctx, "get", f"{RAG_BASE}/{coll}")[0] == 200)
    c = ctx.api.get(f"{RAG_BASE}/{coll}")
    ctx.caption("Collection", f"{coll}: embedded with {c.get('embedding_model')}"
                + (f", reranked with {c.get('rerank_model')}" if c.get("rerank_model") else "")
                + ". Untrusted by default: any finding other than a link quarantines a document.", stage="Screen", hold=3)
    ui_docs = os.environ.get("DEMO_LAW_UI_DOCS", "fr-code-civil-formation,upload-contract-update-untrusted").split(",")
    try:
        goto(ctx, f"/rag?tenant={TENANT}&collection={coll}")
        region = page.get_by_role("region", name=f"Documents of {coll}")
        if not region.count():
            ctx.click(page.get_by_role("button", name=f"Show the documents of {coll}"))
    except Exception:  # noqa: BLE001
        region = page.get_by_role("region", name=f"Documents of {coll}")
    for d in m["documents"]:
        content = law.read_text(f"rag/{d['file']}")
        doc = {"doc_id": d["doc_id"], "title": d["title"], "content": content, "content_type": "text/markdown",
               "source": d["source"]}
        if d.get("trust"):
            doc["trust"] = d["trust"]
        if d["doc_id"] in ui_docs:
            if d.get("poisoned"):
                ctx.caption("Ingest", "A client upload from the portal, untrusted, on contract formation: it hides an "
                            "instruction to the model and an exfiltration request.", stage="Screen", hold=2)

            def ui(d=d, content=content):
                ctx.click(region.get_by_role("button", name="Ingest document"))
                form = page.get_by_role("form", name="Ingest document")
                form.wait_for()
                box = lab(form, "Text")
                ctx.point(box)
                box.fill(content)
                ctx.type(lab(form, "Document id"), d["doc_id"])
                lab(form, "Format").select_option("text/markdown")
                ctx.type(lab(form, "Title"), d["title"])
                ctx.type(lab(form, "Source"), d["source"])
                lab(form, "Trust").select_option(d.get("trust") or "")
                ctx.click(form.get_by_role("button", name="Ingest document"))
                form.wait_for(state="hidden")
            console_step(ctx, f"ingest {d['doc_id']}", ui,
                         lambda doc=doc: ctx.api.post(f"{RAG_BASE}/{coll}/documents", doc), "Ingest", "Ground",
                         verify=lambda d=d: api_try(ctx, "get", f"{RAG_BASE}/{coll}/documents/{d['doc_id']}")[0] == 200)
        else:
            ctx.api.post(f"{RAG_BASE}/{coll}/documents", doc)
    rest = len([x for x in m["documents"] if x["doc_id"] not in ui_docs])
    api_note(ctx, "Ingest", f"The other {rest} documents, ingested the same way from the firm's document store.",
             "POST /api/v1/tenants/{t}/rag/collections/{c}/documents in a loop", "Ground", hold=3)
    timeout = int(os.environ.get("E2E_RAG_INDEX_TIMEOUT_S", "600"))
    with ctx.idle("indexing"):
        ctx.caption("Indexing", "Each document is cut along its headings, scanned for injections, pseudonymised, embedded "
                    "through the gateway and indexed.", stage="Ground", clock=True)

        def settled():
            out = {}
            for d in m["documents"]:
                v = ctx.api.get(f"{RAG_BASE}/{coll}/documents/{d['doc_id']}")
                newest = (v.get("versions") or [{}])[0]
                if newest.get("status") not in TERMINAL_DOC:
                    return None
                out[d["doc_id"]] = {"status": newest.get("status"), "live_version": v.get("live_version"),
                                    "chunks": newest.get("chunks"),
                                    "findings": sorted({x.get("kind") for x in newest.get("findings") or []})}
            return out
        docs = wait("the documents to be indexed", settled, timeout=timeout)
    st["rag"] = {"collection": coll, "embedding_model": c.get("embedding_model"), "rerank_model": c.get("rerank_model"),
                 "documents": docs}
    st_save(st)
    try:
        page.reload()
    except Exception:  # noqa: BLE001
        pass
    ready = sum(1 for x in docs.values() if x["status"] == "ready")
    poisoned = [d["doc_id"] for d in m["documents"] if d.get("poisoned")]
    pz = docs.get(poisoned[0], {}) if poisoned else {}
    ctx.caption("Vetting", f"{ready} documents ready and served. The client upload is {pz.get('status')}, findings "
                f"{', '.join(pz.get('findings') or []) or 'none'}: it is never served unless an administrator releases it.",
                stage="Screen", hold=4)
    for p in poisoned:
        try:
            btn = region.get_by_role("button", name=f"Review {p}")
            if btn.count():
                ctx.click(btn)
                ctx.pause(4)
                page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass


def create_version(ctx, name, spec, caption_line):
    """A node version through the console's node form (schema-checked live), else the admin API."""
    page = ctx.page
    base = f"{NODE_BASE}/{name}"
    status, listed = api_try(ctx, "get", f"{base}/versions")
    existing = (listed or {}).get("versions", []) if status == 200 else []
    same = [v for v in existing if v["spec"] == spec and v["state"] != "retired"]
    if not same:
        def ui():
            if existing:
                goto(ctx, f"/nodes/{TENANT}/{name}")
                ctx.click(page.get_by_role("button", name="New version"))
                form = page.get_by_role("form", name="Create version")
            else:
                nav(ctx, "Nodes", "/nodes")
                ctx.click(page.get_by_role("button", name="New node").first)
                form = page.get_by_role("form", name="Create node")
                lab(form, "Tenant").select_option(TENANT)
                ctx.type(lab(form, "Name"), name)
            box = form.locator("#node-spec")
            ctx.point(box)
            box.fill(json.dumps(spec, indent=2, ensure_ascii=False))
            form.get_by_text("Valid against node.schema.json").wait_for()
            ctx.caption("Nodes", caption_line, stage="Nodes", hold=3)
            ctx.click(form.get_by_role("button", name=re.compile(r"^Create (node|draft)$")))
            form.wait_for(state="hidden")
        console_step(ctx, f"node {name}", ui, lambda: ctx.api.post(f"{base}/versions", {"spec": spec}), "Nodes", "Nodes",
                     verify=lambda: any(v["spec"] == spec for v in (api_try(ctx, "get", f"{base}/versions")[1] or {}).get(
                         "versions") or []))
    else:
        ctx.caption("Nodes", caption_line, stage="Nodes", hold=3)
    listed = ctx.api.get(f"{base}/versions")
    v = [x for x in listed["versions"] if x["spec"] == spec and x["state"] != "retired"][-1]
    if v["state"] == "draft":
        ver = v["version"]

        def ui():
            goto(ctx, f"/nodes/{TENANT}/{name}")
            ctx.click(page.get_by_role("button", name=f"Publish v{ver}"))
            dlg = ctx.dialog(f"Publish {name} v{ver}?")
            live_box = lab(dlg, "Make it the live version")
            if live_box.is_checked():
                ctx.click(live_box)
            ctx.click(dlg.get_by_role("button", name="Publish", exact=True))
            dlg.wait_for(state="hidden")
        console_step(ctx, f"publish {name} v{ver}", ui,
                     lambda: ctx.api.post(f"{base}/versions/{ver}/publish", {"promote": False}), "Publish", "Nodes",
                     verify=lambda: ctx.api.get(f"{base}/versions/{ver}").get("state") == "published")
        v = ctx.api.get(f"{base}/versions/{ver}")
    return v


def run_eval(ctx, name, ver, body, label):
    e = ctx.api.post(f"{NODE_BASE}/{name}/versions/{ver}/evals", body)
    return e["id"]


def finish_evals(ctx, started, label):
    timeout = int(os.environ.get("E2E_EVAL_TIMEOUT_S", "1800"))
    out = {}
    with ctx.idle(label):
        for key, eid in started.items():
            out[key] = wait(f"eval {eid}", lambda eid=eid: (lambda b: b if b["status"] in ("succeeded", "failed", "cancelled")
                                                           else None)(ctx.api.get(f"/api/v1/tenants/{TENANT}/evals/{eid}")),
                            timeout=timeout)
            ctx.record_eval(key, out[key])
    return out


def eval_line(e):
    s = e.get("summary") or {}
    if e.get("suite") == "injection":
        return (f"injection {e['status']}: {s.get('blocked', '?')} of {s.get('trials', '?')} attacks blocked"
                + (f", the model followed {s.get('attacked')}" if s.get("attacked") is not None else ""))
    return (f"golden {e['status']}: pass^k {s.get('pass_hat_k')}, cost per success {s.get('cost_per_success_usd')} USD, "
            f"p95 {((s.get('latency_ms') or {}).get('p95'))} ms")


def promote(ctx, name, ver):
    page = ctx.page
    base = f"{NODE_BASE}/{name}"
    g = ctx.api.get(f"{base}/versions/{ver}/gate")
    if not g.get("allowed"):
        ctx.caption("Gate", f"The gate refuses {name} v{ver}: "
                    + "; ".join(p.get("message", "") for p in g.get("problems") or []), stage="Evals", hold=6)
        raise RuntimeError(f"the gate refused {name} v{ver}: {json.dumps(g)[:800]}")
    info = ctx.api.get(f"{base}/versions")
    if info.get("live") == ver:
        return g

    def ui():
        goto(ctx, f"/nodes/{TENANT}/{name}")
        ctx.click(page.get_by_role("button", name=f"Make v{ver} live").first)
        dlg = ctx.dialog(f"Make {name} v{ver} live?")
        ctx.click(dlg.get_by_role("button", name="Make live"))
        dlg.wait_for(state="hidden")
    console_step(ctx, f"make {name} v{ver} live", ui, lambda: ctx.api.post(f"{base}/promote", {"version": ver}),
                 "Gate", "Evals", verify=lambda: ctx.api.get(f"{base}/versions").get("live") == ver)
    return g


def subnodes(ctx):
    """matter-desk and research: import, injection evals, gate"""
    st = st_load()
    desk = law.render_spec("matter-desk", ontology_version=st["ontology_version"])
    research = law.render_spec("research")
    vd = create_version(ctx, "matter-desk", desk,
                        "matter-desk: a router picks the topic (deadlines, status, billing, conflict check), one of four "
                        "fixed CQIR queries runs, an answer vertex replies in the node's JSON schema. PII floor "
                        "reversible: the answer may quote a client, the model never sees one.")
    ctx.panel(card_html("matter-desk: the deadlines query in its spec", "typed CQIR, ontology ids only; the model never "
                        "writes a query", "<pre>" + esc(json.dumps(next(v for v in desk["graph"]["vertices"]
                                                                          if v["id"] == "deadlines")["config"]["args"]["query"],
                                                                     indent=1, ensure_ascii=False)[:1400]) + "</pre>"))
    ctx.caption("Nodes", f"The queries name ontology version {st['ontology_version']}, the one just approved. Open "
                "deadlines join each matter's client through the verified reference.", stage="Ground", hold=6)
    ctx.hide_panel()
    vr = create_version(ctx, "research", research,
                        f"research: builtin://rag_search over rag.{law.COLLECTION}:read, then an answer that cites each "
                        "passage it uses as [n]. PII floor mask: no client data is needed to answer a question of law.")
    st["nodes"] = {"matter-desk": {"version": vd["version"], "hash": vd["hash"]},
                   "research": {"version": vr["version"], "hash": vr["hash"]}}
    st_save(st)
    goto(ctx, f"/nodes/{TENANT}/matter-desk?tab=evals&ev={vd['version']}")
    started = {
        "matter-desk injection": run_eval(ctx, "matter-desk", vd["version"], {
            "suite": "injection", "input": {"question": "Quels sont les délais à venir sur le dossier Maison Delorme ?",
                                            "task": "matter"}}, "matter-desk"),
        "research injection": run_eval(ctx, "research", vr["version"], {
            "suite": "injection", "input": {"question": "Que dit le Code civil sur l'offre et l'acceptation ?"}},
            "research"),
    }
    api_note(ctx, "Injection suite", "Both read-only nodes go through the prompt injection suite before they may go live: "
             "attacks planted in what they read (rows, passages) try to steer the answer.",
             "POST .../nodes/{n}/versions/{v}/evals with a raw input (the console's dialog takes a golden case)",
             "Evals", hold=2)
    ctx.caption("Injection suite", "Each attack is an ordinary run of the version against sandboxed tools: nothing real "
                "is called.", stage="Evals", clock=True)
    res = finish_evals(ctx, started, "injection evals")
    try:
        ctx.page.reload()
    except Exception:  # noqa: BLE001
        pass
    st["evals"] = {k: {"id": e["id"], "status": e["status"], "summary": e.get("summary")} for k, e in res.items()}
    st_save(st)
    ctx.caption("Injection suite", "; ".join(f"{k.split()[0]}: {eval_line(e)}" for k, e in res.items()) + ".",
                stage="Evals", hold=5)
    for name, v in (("matter-desk", vd), ("research", vr)):
        g = promote(ctx, name, v["version"])
        st.setdefault("gates", {})[name] = g
    st_save(st)
    ctx.caption("Gate", "Both gates passed: the latest injection eval of each version meets the bar. matter-desk and "
                "research are live.", stage="Evals", hold=3)


def orchestrator(ctx):
    """legal-assistant: subnodes, golden set, evals, promotion"""
    st = st_load()
    page = ctx.page
    versions = {k: v["version"] for k, v in st["nodes"].items()}
    spec = law.render_spec("legal-assistant", versions=versions)
    v = create_version(ctx, "legal-assistant", spec,
                       f"legal-assistant: an intake router sends each request to matter-desk v{versions['matter-desk']} "
                       f"(matters, or a conflict check) or to research v{versions['research']}, pinned by version, run as "
                       "subnodes under one budget.")
    ver = v["version"]
    st["nodes"]["legal-assistant"] = {"version": ver, "hash": v["hash"]}
    st_save(st)
    golden = law.load("node/golden.json")
    bar = {"golden": {"min_pass_k": float(os.environ.get("E2E_LAW_MIN_PASS_K", "0.8")), "min_k": 3,
                      "max_cost_per_success_usd": 0.05,
                      "max_p95_latency_ms": int(os.environ.get("E2E_LAW_MAX_P95_MS", os.environ.get("E2E_MAX_P95_MS", "90000")))},
           "injection": {"min_pass_rate": 1.0}}
    goto(ctx, f"/nodes/{TENANT}/legal-assistant?tab=evals&ev={ver}")
    ctx.caption("Evals", "The promotion bar of the orchestrator: golden cases run three times each, the injection suite, "
                "and its pinned subnodes must have passed their own gates.", stage="Evals")

    def bar_ui():
        ctx.click(page.get_by_role("button", name="Edit bar"))
        dlg = ctx.dialog("Promotion bar of legal-assistant")
        ctx.type(lab(dlg, "Least pass^k"), f"{bar['golden']['min_pass_k'] * 100:g}")
        ctx.type(lab(dlg, "Least k"), "3")
        ctx.type(lab(dlg, "Most cost per success"), "0.05")
        ctx.type(lab(dlg, "Most p95 latency"), str(bar["golden"]["max_p95_latency_ms"]))
        ctx.type(lab(dlg, "Least share of attacks blocked"), "100")
        lab(dlg, "Read-only versions").select_option("required")
        ctx.click(dlg.get_by_role("button", name="Save bar"))
        dlg.wait_for(state="hidden")
    console_step(ctx, "the promotion bar", bar_ui, lambda: ctx.api.put(f"{NODE_BASE}/legal-assistant/eval-policy", bar),
                 "Evals", "Evals", verify=lambda: law.covers((lambda p: p.get("policy", p))(
                     ctx.api.get(f"{NODE_BASE}/legal-assistant/eval-policy")), bar))

    def golden_ui():
        ctx.click(page.get_by_role("button", name=re.compile(r"^(Create golden set|Edit cases)$")))
        dlg = ctx.dialog(f"Golden set of legal-assistant v{ver}")
        box = lab(dlg, "Cases")
        ctx.point(box)
        box.fill(json.dumps(golden["cases"], indent=2, ensure_ascii=False))
        ctx.pause(1.5)
        ctx.click(dlg.get_by_role("button", name="Save golden set"))
        dlg.wait_for(state="hidden")
    console_step(ctx, "the golden set", golden_ui,
                 lambda: ctx.api.put(f"{NODE_BASE}/legal-assistant/versions/{ver}/golden-set", golden), "Evals", "Evals",
                 verify=lambda: len((api_try(ctx, "get", f"{NODE_BASE}/legal-assistant/versions/{ver}/golden-set")[1]
                                     or {}).get("cases") or []) == len(golden["cases"]))
    ctx.caption("Golden set", f"{len(golden['cases'])} golden cases in French and English: deadlines, billing, a conflict "
                "check and research, each checked by schema, by expected references and by a judge model.",
                stage="Evals", hold=3)
    started = {}
    for suite, body in (("golden", {"suite": "golden", "k": 3, "concurrency": 4, "judge_model": law.CHAT_ID}),
                        ("injection", {"suite": "injection", "input_case": "fr-deadlines-delorme"})):
        before = {e["id"] for e in (ctx.api.get(f"/api/v1/tenants/{TENANT}/evals").get("data") or [])}

        def ui(suite=suite):
            ctx.click(page.get_by_role("button", name="Start eval").first)
            dlg = ctx.dialog(f"Start an eval of legal-assistant v{ver}")
            lab(dlg, "Suite").select_option(suite)
            if suite == "golden":
                ctx.type(lab(dlg, "k (runs per case)"), "3")
                ctx.type(lab(dlg, "Concurrency"), "4")
                ctx.type(lab(dlg, "Judge model"), law.CHAT_ID)
            else:
                lab(dlg, "Run input").select_option("fr-deadlines-delorme")
            ctx.click(dlg.get_by_role("button", name="Start eval"))
            dlg.wait_for(state="hidden")
            new = wait("the eval to be listed", lambda: [e for e in (ctx.api.get(
                f"/api/v1/tenants/{TENANT}/evals").get("data") or []) if e["id"] not in before], timeout=30)
            started[suite] = new[0]["id"]
            ctx.page.keyboard.press("Escape")

        def api(suite=suite, body=body):
            started[suite] = run_eval(ctx, "legal-assistant", ver, body, suite)
        console_step(ctx, f"start the {suite} eval", ui, api, "Evals", "Evals")
    ctx.caption("Evals", "Both evals are running: every trial is a real run of the orchestrator and its subnodes, "
                "through the gateway, metered as the tenant's eval key.", stage="Evals", clock=True)
    res = finish_evals(ctx, {f"legal-assistant {k}": v for k, v in started.items()}, "orchestrator evals")
    st["evals"].update({k: {"id": e["id"], "status": e["status"], "summary": e.get("summary")} for k, e in res.items()})
    st_save(st)
    try:
        page.reload()
    except Exception:  # noqa: BLE001
        pass
    ctx.caption("Evals", "; ".join(eval_line(e) for e in res.values()) + ".", stage="Evals", hold=5)
    g = promote(ctx, "legal-assistant", ver)
    st.setdefault("gates", {})["legal-assistant"] = g
    st_save(st)
    subs = (g.get("evidence") or {}).get("subnodes") or {}
    ctx.caption("Gate", "The gate: the golden and injection evals of this version meet the bar"
                + (f", and its {len(subs)} pinned subnode versions passed their own gates" if subs else "")
                + ". legal-assistant is live.", stage="Evals", hold=4)


def intents(ctx):
    """caliban/auto: the legal intent"""
    st = st_load()
    page = ctx.page
    exemplars = law.load("config/exemplars.json")["intents"]
    goto(ctx, f"/tenants/{TENANT}?tab=intents")
    ctx.caption("Routing intents", "caliban/auto classifies each request by its nearest exemplars, in French and English. "
                "The firm's legal intent gets its own; routers pick it up without a restart.", stage="Route")
    listed = {i["intent"]: i for i in (ctx.api.get(f"/api/v1/tenants/{TENANT}/intents").get("intents") or [])}
    for name, ex in exemplars.items():
        have = listed.get(name)
        if have and have.get("source") == "api" and have.get("exemplars") == ex:
            continue

        def ui(name=name, ex=ex, have=have):
            if have:
                ctx.click(page.get_by_role("table", name="Routing intents").get_by_role("button", name=name, exact=True))
                form = page.get_by_role("form", name=f"Intent {name}")
            else:
                ctx.click(page.get_by_role("button", name="New intent"))
                form = page.get_by_role("form", name="New intent")
                ctx.type(lab(form, "Intent"), name)
            box = lab(form, "Exemplars")
            ctx.point(box)
            box.fill("\n".join(ex))
            ctx.pause(2)
            ctx.click(form.get_by_role("button", name="Save"))
            form.wait_for(state="hidden")
        console_step(ctx, f"intent {name}", ui,
                     lambda name=name, ex=ex: ctx.api.put(f"/api/v1/tenants/{TENANT}/intents/{name}", {"exemplars": ex}),
                     "Routing intents", "Route", verify=lambda name=name, ex=ex: any(
                         i["intent"] == name and i.get("exemplars") == ex
                         for i in ctx.api.get(f"/api/v1/tenants/{TENANT}/intents").get("intents") or []))
    ctx.api.patch(f"/api/v1/tenants/{TENANT}", {"node_routes": {law.INTENT: "node/legal-assistant@published"}})
    try:
        goto(ctx, f"/tenants/{TENANT}?tab=intents")
    except Exception:  # noqa: BLE001
        pass
    api_note(ctx, "Node route", "The legal intent hands to node/legal-assistant@published: always the live version.",
             "PATCH /api/v1/tenants/{t} node_routes (the console shows the mapping, cannot edit it yet)", "Route")
    listed = ctx.api.get(f"/api/v1/tenants/{TENANT}/intents")
    st["intents"] = {i["intent"]: {"exemplars": len(i.get("exemplars") or []), "source": i.get("source"),
                                   "node": i.get("node")} for i in listed.get("intents") or []}
    st_save(st)
    # Routers apply the intents with their next snapshot and rebuild their kNN index (embedding the
    # exemplars): wait until the data plane serves the control plane's current config version, then
    # a few seconds for the index. A wait for correctness, not for the viewer.
    with ctx.idle("intents settle"):
        ctx.caption("Routing intents", "Routers pick up the change with their next signed snapshot and rebuild "
                    "their index.", stage="Route", clock=True)
        want = (api_try(ctx, "get", "/api/v1/health")[1] or {}).get("config_version")
        router = getattr(ctx, "gateway_url", os.environ.get("E2E_ROUTER_URL", "http://127.0.0.1:18080")).rstrip("/")

        def served():
            h = law.http("GET", router + "/healthz", ok=None, timeout=10)[2] or {}
            return want and (h.get("config_version") == want or (h.get("snapshot") or {}).get("version") == want)
        try:
            law.wait_until("the routers' snapshot", served, timeout=int(os.environ.get("E2E_LAW_SNAPSHOT_TIMEOUT_S", "120")),
                           every=1.0)
        except TimeoutError:
            print("   the data plane did not report the new config version; waiting the settle time only", flush=True)
        time.sleep(int(os.environ.get("E2E_INTENT_SETTLE_S", "8")))


def apikey(ctx):
    """An API key for the firm's intranet"""
    st = st_load()
    page = ctx.page
    if st.get("api_key"):
        ctx.caption("API key", f"The intranet's key {st.get('api_key_id')} exists already.", stage="Clear", hold=2)
        return
    goto(ctx, f"/tenants/{TENANT}?tab=api-keys")
    ctx.caption("API key", "A key for the firm's intranet, allowed to run legal-assistant only. Caliban stores only its "
                "SHA-256 hash; the key is blurred in this recording.", stage="Clear")
    out = {}

    def ui():
        ctx.type(lab(page, "Key name"), law.APP_KEY_NAME)
        ctx.click(page.locator("summary").filter(has_text="Node access"))
        ctx.click(lab(page, "Only these nodes"))
        group = page.get_by_role("group", name="Nodes")
        box = group.get_by_label("legal-assistant", exact=True) if group.count() else None
        if box is not None and box.count():
            ctx.click(box)
        else:
            ctx.type(page.get_by_label(re.compile(r"^(Other node names|Node names)$")), "legal-assistant")
        ctx.click(page.get_by_role("button", name="Mint key"))
        notice = page.get_by_role("region", name="New API key")
        notice.wait_for()
        key = notice.get_by_test_id("plaintext-key").inner_text().strip()
        ctx.point(notice, 2.5)
        keys = ctx.api.get(f"/api/v1/tenants/{TENANT}/api-keys")
        match = [k for k in keys if k.get("name") == law.APP_KEY_NAME and key.startswith(k.get("prefix") or "\0")]
        ctx.click(notice.get_by_role("button", name="I've stored it"))
        out.update(key=key, id=match[-1]["id"] if match else None)

    def api():
        k = ctx.api.post(f"/api/v1/tenants/{TENANT}/api-keys", {"name": law.APP_KEY_NAME, "nodes": ["legal-assistant"]})
        out.update(key=k["key"], id=k["id"])
    console_step(ctx, "mint the intranet key", ui, api, "API key", "Clear", verify=lambda: bool(out.get("key")))
    st.update(api_key=out["key"], api_key_id=out["id"])
    st_save(st)


# ─────────────────────────── the requests ───────────────────────────


def model_log_offsets():
    return {p: os.path.getsize(p) for p in law.model_logs()}


def model_saw(offsets, marker):
    """What the model received for the first call after `offsets` that carries `marker`."""
    for p in law.model_logs():
        with open(p, encoding="utf-8") as f:
            f.seek(offsets.get(p, 0))
            lines = f.read().splitlines()
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            msgs = entry.get("messages") or []
            text = "\n".join(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
                             for m in msgs)
            if marker in text:
                users = [m.get("content") for m in msgs if m.get("role") == "user" and isinstance(m.get("content"), str)]
                u = users[-1] if users else text
                return u[:1100]
    return None


def answer_extra(parsed, run_id):
    if not isinstance(parsed, dict):
        return ""
    rows = [("kind", parsed.get("kind")), ("language", parsed.get("language"))]
    if parsed.get("matters"):
        rows.append(("matters", ", ".join(parsed["matters"])))
    if parsed.get("conflict") and parsed.get("conflict") != "not_applicable":
        rows.append(("conflict", parsed["conflict"]))
    for c in parsed.get("citations") or []:
        rows.append((f"[{c.get('n')}]", c.get("doc_id")))
    rows.append(("run", run_id))
    return "<h4>Structured answer</h4>" + table_html(rows)


def follow(ctx, key, run_id, timeout=900):
    return wait(f"run {run_id}", lambda: (lambda r: r if isinstance(r, dict) and r.get("status") in TERMINAL_RUN else None)(
        ctx.gateway(f"/v1/runs/{run_id}", None, key)[2]), timeout=timeout, every=1.5)


def run_tree_html(run):
    rows = []
    for s in run.get("steps") or []:
        depth = (s.get("id") or "").count(">")
        rows.append(("  " * depth + ("> " if depth else "") + (s.get("vertex") or s.get("id") or ""),
                     s.get("kind"), s.get("status"), f"{s.get('duration_ms')} ms",
                     ",".join(s.get("labels") or []) or "-"))
    return table_html(rows, ["step", "kind", "status", "time", "taint labels"])


def send(ctx, st, req):
    key = st["api_key"]
    body = {"model": req.get("model", "caliban/auto"), "messages": [{"role": "user", "content": req["text"]}]}
    if req.get("untrusted"):
        body["caliban"] = {"untrusted": True}
    title = f"{req['id']}: {req['title']}"
    ctx.client_panel(title, body, None, None, None, pending="the intranet sends it to Caliban…")
    ctx.pause(2.5)
    offsets = model_log_offsets()
    status, h, b = ctx.gateway("/v1/chat/completions", body, key, label=req["label"])
    run_id = h.get("caliban-run-id") or (((b.get("caliban") or {}).get("run_id")) if isinstance(b, dict) else None)
    content = ""
    if isinstance(b, dict) and b.get("choices"):
        content = ((b["choices"][0] or {}).get("message") or {}).get("content") or ""
    run = None
    if status == 504 and run_id:
        run = follow(ctx, key, run_id)
        ctx.run_timing(run_id, key, label=req["label"])
        out = run.get("output")
        content = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    parsed = law.parse_json(content)
    marker = "[law:research-answer]" if (parsed or {}).get("kind") == "legal_research" else "[law:desk-answer]"
    saw = model_saw(offsets, marker)
    shown = {k: v for k, v in h.items() if k.startswith("x-caliban") or k.startswith("caliban-")}
    ctx.client_panel(title, body, status, shown, b, answer=(parsed or {}).get("answer") or content,
                     extra=answer_extra(parsed, run_id), model_saw=saw)
    return {"id": req["id"], "status": status, "headers": shown, "run_id": run_id, "answer": parsed or content,
            "model_saw_masked": saw}


def console_run(ctx, run_id):
    return api_try(ctx, "get", f"/api/v1/tenants/{TENANT}/runs/{run_id}")[1] or {}


def show_run(ctx, run_id, lines):
    """The run in the console, then the facts the captions state (from the run's API view)."""
    ctx.hide_panel()
    try:
        goto(ctx, f"/runs/{TENANT}/{run_id}")
        steps = ctx.page.get_by_role("table", name="Steps")
        steps.wait_for()
        ctx.point(steps, 1)
    except Exception:  # noqa: BLE001
        pass
    for title, line, target, stage in lines:
        try:
            if target:
                loc = ctx.page.get_by_text(target, exact=True)
                if loc.count():
                    ctx.point(loc.first, 0.6)
        except Exception:  # noqa: BLE001
            pass
        ctx.caption(title, line, stage=stage, hold=4.5)


def search_results(run):
    out = []
    for s in run.get("steps") or []:
        if s.get("kind") == "tool" and s.get("vertex") == "search":
            o = s.get("output")
            o = law.parse_json(o) if not isinstance(o, dict) else o
            out += (o or {}).get("results") or []
    return out


def warm_routing(ctx, key):
    """caliban/auto rebuilds its kNN index in the background, started by the first request after a
    snapshot changed the intents (core caliban-route `needs_refresh`): that first request is still
    classified with the previous index. So before the recorded requests, one short out-of-scope
    request (not recorded, not in the telemetry) starts the rebuild, then a pause lets it finish
    (embedding the exemplars: tens of milliseconds locally, under a few seconds on the testbed)."""
    router = getattr(ctx, "gateway_url", os.environ.get("E2E_ROUTER_URL", "http://127.0.0.1:18080")).rstrip("/")
    body = {"model": "caliban/auto", "max_tokens": 8,
            "messages": [{"role": "user", "content": "Write one line about the sea at dawn."}]}
    try:
        law.http("POST", router + "/v1/chat/completions", key, body, ok=None, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"   routing warm-up: {type(e).__name__}: {e}", flush=True)
    time.sleep(int(os.environ.get("E2E_LAW_KNN_SETTLE_S", "10")))


def requests(ctx):
    """Seven requests from the firm's intranet"""
    st = st_load()
    reqs = law.load("config/requests.json")["requests"]
    with ctx.idle("routing warm-up"):
        warm_routing(ctx, st["api_key"])
    ctx.caption("Requests", "The firm's intranet sends plain OpenAI chat completions to caliban/auto with its key, no "
                "Caliban-specific field. Its 'analyse a client e-mail' button names the node instead and marks the "
                "e-mail as untrusted caller data.", stage="Route", hold=4)
    results = []
    for req in reqs:
        ctx.caption(req["id"], req["title"], stage="Route")
        rec = send(ctx, st, req)
        h = rec["headers"]
        ctx.pause(3)
        how = (f"Intent {(h.get('x-caliban-intent') or 'none').split(';')[0]}, handed to " if req.get("model") is None
               else f"Model {req['model']} (no classification), run by ")
        ctx.caption(req["id"], how +
                    f"{h.get('x-caliban-routed-model') or h.get('x-caliban-route')}; personal data entities protected "
                    f"in its model calls: {h.get('x-caliban-pii-entities', '?')}; billed {h.get('x-caliban-billed-usd', '?')} "
                    "USD.", stage="Route", hold=4)
        run = console_run(ctx, rec["run_id"]) if rec.get("run_id") else {}
        rec["run"] = {k: run.get(k) for k in ("status", "cost_usd", "model_cost_usd", "usage", "answer_check",
                                              "input_labels", "stop_reason", "origin")}
        rec["steps"] = [{k: s.get(k) for k in ("id", "vertex", "kind", "status", "duration_ms", "labels")}
                        for s in run.get("steps") or []]
        if rec.get("model_saw_masked"):
            ctx.caption(req["id"], "Left, what the model received: names from the firm's dictionary, e-mail addresses, "
                        "phone numbers and IDs arrive as surrogates; the lawyer reads the real values.", stage="Screen",
                        hold=4)
        subs = [s for s in rec["steps"] if ">" in (s.get("id") or "")]
        if req["id"] == "L1" and run:
            ctx.panel(card_html(f"Run {rec['run_id']}: the subnode tree", "from the run's steps (the console's run API)",
                                run_tree_html(run)))
            ctx.caption("L1", f"intake chose the matter desk; {len(subs)} steps ran inside the matter-desk subnode: its "
                        "topic router, one typed query, the answer. The query result carries the datasource taint label.",
                        stage="Ground", hold=6)
            u = run.get("usage") or {}
            show_run(ctx, rec["run_id"], [
                ("L1", f"Personal data protected across the run's model calls: {u.get('pii_entities')} entities.",
                 "Personal data", "Screen"),
                ("L1", f"Cost: billed {run.get('cost_usd')} USD as caliban/auto, the model's real cost "
                 f"{run.get('model_cost_usd')} USD metered next to it.", "Model cost", "Log")])
        if req["id"] in ("L3", "L6") and run:
            res = search_results(run)
            cited = sorted({c.get("doc_id") for c in (rec["answer"].get("citations") or [])}) if isinstance(rec["answer"], dict) else []
            rec["search_results"] = [{k: r.get(k) for k in ("n", "doc_id", "doc_version", "chunk_id", "score")} for r in res]
            ctx.panel(card_html(f"{req['id']}: what rag_search returned", "passages framed as data, with citations",
                                table_html([(f"[{r.get('n')}]", r.get("doc_id"), r.get("chunk_id"), r.get("score"))
                                            for r in res], ["n", "document", "chunk", "score"])))
            poisoned = any((r.get("doc_id") or "").startswith("upload-") for r in res)
            ctx.caption(req["id"], f"{len(res)} passages, {'including' if poisoned else 'none from'} the quarantined client "
                        f"upload; the answer cites {', '.join(cited) or 'nothing'}.", stage="Ground", hold=6)
            ctx.hide_panel()
        if req["id"] == "L4":
            a = rec["answer"] if isinstance(rec["answer"], dict) else {}
            ctx.caption("L4", f"Conflict check verdict: {a.get('conflict')}, matters {', '.join(a.get('matters') or []) or 'none'}: "
                        "computed from every party of every matter, open or closed.", stage="Ground", hold=5)
        if req["id"] == "L5" and run:
            ac = run.get("answer_check") or {}
            findings = ac.get("findings") or []
            labels = ", ".join(run.get("input_labels") or []) or "none"
            show_run(ctx, rec["run_id"], [
                ("L5", f"The run input carries the label {labels}: the e-mail is data. The answer guard checked the answer "
                 f"against it: action {ac.get('action')}, {len(findings)} findings"
                 + (f" ({', '.join(sorted({f.get('rule') or f.get('kind') or '?' for f in findings}))})" if findings else "")
                 + ".", "Answer guard", "Screen")])
            text = json.dumps(rec["answer"], ensure_ascii=False)
            leaked = [m for m in req.get("attack") or [] if m in text]
            ctx.caption("L5", "What reached the lawyer carries " + ("none of the attacker's contacts or instructions."
                        if not leaked else f"these attack markers: {', '.join(leaked)}."), stage="Screen", hold=5)
        results.append(rec)
        st["requests"] = results
        st_save(st)
    ctx.hide_panel()


def close(ctx):
    """Usage and audit"""
    try:
        goto(ctx, f"/usage?tenant={TENANT}")
        ctx.caption("Usage", "One usage event per model call: tokens, cache outcome, personal data counted, the real cost "
                    "and what caliban/auto billed. Retrieval's own calls are metered apart. No prompt or answer is kept.",
                    stage="Log", hold=4)
        ctx.page.mouse.wheel(0, 700)
        ctx.pause(3)
    except Exception:  # noqa: BLE001
        pass
    a = ctx.api.get(f"/api/v1/audit?limit=500")
    entries = [e for e in a.get("entries") or [] if e.get("tenant_id") == TENANT or (e.get("target") or "").startswith(TENANT)]
    actions = sorted({e.get("action") for e in entries})
    try:
        goto(ctx, f"/audit?tenant={TENANT}")
        chain = ctx.page.get_by_role("region", name="Chain integrity")
        if chain.count():
            ctx.point(chain.first, 1)
    except Exception:  # noqa: BLE001
        pass
    ctx.caption("Audit", f"The hash-chained audit log: chain verified {a.get('chain_verified')}; this walkthrough wrote "
                f"{len(entries)} entries for the firm, among them {', '.join(actions[:8])}.", stage="Log", hold=5)
    st = st_load()
    st["audit"] = {"chain_verified": a.get("chain_verified"), "actions": actions}
    st_save(st)
