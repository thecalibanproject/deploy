"""The support app's agents, as code: node specs (core schemas/node.schema.json) and golden sets.

Three subagents and an orchestrator (research note 04: explicit graphs, plan then execute):

  billing-agent         workflow: one model call, constrained to a JSON schema (refund policy in the prompt)
  bug-triage            workflow: one model call, constrained (severity, component, known issue)
  docs-helper           agent: searches the product docs (builtin://rag_search), answers with citations
  support-dispatch      workflow without a model: picks the subagent a task names (schema checks)
  support-orchestrator  workflow: plan (constrained) -> one dispatch per task, in parallel (map)
                        -> one merged reply (constrained)

The plan is fixed before any subagent output exists, so what a subagent returns cannot change which
subagents run (control-flow integrity, the CaMeL / plan-then-execute pattern of note 04). Every model
vertex runs with reasoning off and constrained decoding (structured_output: json_schema), which vLLM
serves with guided decoding (note 02, "When to Reason"; note 08 on small models and schemas).
"""
COLLECTION = "product_docs"
AGENTS = ("billing", "bug", "docs")
SUBAGENT = {"billing": "billing-agent", "bug": "bug-triage", "docs": "docs-helper"}
LANG = {"enum": ["en", "fr", "ar"]}

REQUEST = {"type": "object", "required": ["request"], "additionalProperties": False,
           "properties": {"request": {"type": "string", "minLength": 1, "maxLength": 2000}}}

BILLING_OUT = {
    "type": "object", "additionalProperties": False,
    "required": ["issue", "summary", "next_steps", "refund_eligible", "language"],
    "properties": {
        "issue": {"enum": ["double_charge", "refund", "invoice", "payment_method", "plan_change", "other"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 200},
        "next_steps": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "maxLength": 240}},
        "refund_eligible": {"type": "boolean"},
        "language": LANG,
    },
}
BUG_OUT = {
    "type": "object", "additionalProperties": False,
    "required": ["severity", "component", "title", "known_issue", "workaround", "language"],
    "properties": {
        "severity": {"enum": ["critical", "high", "medium", "low"]},
        "component": {"enum": ["export", "editor", "sync", "auth", "boards", "other"]},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "known_issue": {"type": "string", "maxLength": 20},
        "workaround": {"type": "string", "maxLength": 300},
        "language": LANG,
    },
}
DOCS_OUT = {
    "type": "object", "required": ["answer", "citations", "language"],
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 1500},
        "citations": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}, "doc_id": {"type": "string"}}}},
        "language": LANG,
    },
}
TASK = {"type": "object", "required": ["agent", "request"], "additionalProperties": False,
        "properties": {"agent": {"enum": list(AGENTS)},
                       "request": {"type": "string", "minLength": 1, "maxLength": 1000}}}
PLAN = {"type": "object", "required": ["language", "tasks"], "additionalProperties": False,
        "properties": {"language": LANG, "tasks": {"type": "array", "minItems": 1, "maxItems": 3, "items": TASK}}}
REPLY = {"type": "object", "required": ["reply", "handled_by", "language"], "additionalProperties": False,
         "properties": {"reply": {"type": "string", "minLength": 1, "maxLength": 3000},
                        "handled_by": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"enum": list(AGENTS)}},
                        "language": LANG}}

BILLING_SYSTEM = """[dev:billing] You are the billing specialist of Acme Boards, a fictional SaaS product.
Policy:
- A duplicate charge is refunded automatically within 5 to 7 business days once the two charges are confirmed on the invoice history page.
- Annual plans are refunded pro rata within 30 days of renewal; monthly plans are not refunded.
- Invoices are under Settings > Billing > Invoice history; the VAT number is set in Settings > Billing > Company details.
- The card is changed in Settings > Billing > Payment method.
Classify the request, summarise it in one sentence, and give at most four concrete next steps from this policy only.
Answer in the language of the request (en, fr or ar)."""

BUG_SYSTEM = """[dev:bug] You triage bug reports for Acme Boards, a fictional SaaS product.
Known issues:
- EXP-311 (export): exporting a report of more than 10,000 rows crashes the app. Workaround: export a shorter date range.
- SYN-208 (sync): calendar sync stops after a password change. Workaround: reconnect the integration in Settings > Integrations.
Severity: critical for data loss or a full outage, high for a crash, medium for a broken feature with a workaround, low otherwise.
Set known_issue to the matching id, or an empty string. Answer in the language of the report (en, fr or ar)."""

DOCS_SYSTEM = """[dev:docs] You answer how-to questions about Acme Boards, a fictional SaaS product, from its documentation only.
Call rag_search with the question first. Answer from the returned passages only, cite them as [n], and say so when they do not answer.
Reply with only a JSON object: {"answer": "...", "citations": [{"n": 1, "doc_id": "..."}], "language": "en|fr|ar"}, in the language of the question."""

PLAN_SYSTEM = """[dev:plan] You are the support orchestrator of Acme Boards, a fictional SaaS product.
Split the customer message into independent tasks, at most one per specialist:
- billing: charges, refunds, invoices, payment methods;
- bug: something is broken, crashes or shows an error;
- docs: how to do something in the product.
Each task's request is the part of the message that specialist needs, in the customer's words. Detect the language (en, fr or ar)."""

REPLY_SYSTEM = """[dev:reply] You write the one reply the customer gets, from the specialists' results only.
Address every task, in the customer's language, in a short friendly paragraph. Do not invent policy, ids or steps that are not in the results.
handled_by lists the specialists whose results you used."""


def _common(description, system, input_schema, output_schema, model, budgets):
    return {
        "description": description,
        "prompt": {"system": system, "input_schema": input_schema, "output_schema": output_schema},
        "model_policy": {"model": model, "reasoning": "off"},
        "budgets": budgets,
        # Customer e-mails and card numbers reach no model in clear, sovereign ones included (note 05):
        # pseudonymised on the way in, restored in the answer.
        "guardrails": {"pii": "reversible", "answer": "redact"},
    }


def llm(vid, prompt, schema, max_tokens=700):
    return {"id": vid, "type": "llm", "config": {"prompt": prompt, "output_schema": schema, "reasoning": "off",
                                                 "structured_output": "json_schema", "max_tokens": max_tokens}}


def billing_agent(model):
    spec = _common("Resolves billing requests: duplicate charges, refunds, invoices, payment methods.",
                   BILLING_SYSTEM, REQUEST, BILLING_OUT, model,
                   {"steps": 4, "tokens": 16000, "wall_clock_s": 180, "usd": 0.05})
    spec.update(kind="workflow", graph={"entry": "resolve", "vertices": [
        llm("resolve", "Customer request:\n<<<\n{{input.request}}\n>>>", BILLING_OUT)], "edges": []})
    return spec


def bug_triage(model):
    spec = _common("Triages bug reports: severity, component, known issue and workaround.",
                   BUG_SYSTEM, REQUEST, BUG_OUT, model,
                   {"steps": 4, "tokens": 16000, "wall_clock_s": 180, "usd": 0.05})
    spec.update(kind="workflow", graph={"entry": "triage", "vertices": [
        llm("triage", "Bug report:\n<<<\n{{input.request}}\n>>>", BUG_OUT)], "edges": []})
    return spec


def docs_helper(model):
    spec = _common("Answers how-to questions from the product documentation, with citations.",
                   DOCS_SYSTEM, REQUEST, DOCS_OUT, model,
                   {"steps": 6, "tokens": 30000, "wall_clock_s": 240, "usd": 0.08})
    spec.update(kind="agent", agent={"max_turns": 3},
                tools=[{"ref": "builtin://rag_search", "effect": "read"}],
                datasources={"scopes": [f"rag.{COLLECTION}:read"]})
    return spec


def support_dispatch(model, versions):
    """No model call: three schema checks pick the subagent the task names. A failed check wraps its
    input as {"previous": ..., "feedback": ...}, hence the nested templates."""
    def is_(agent):
        return {"type": "object", "required": ["agent"], "properties": {"agent": {"const": agent}}}

    def sub(vid, agent, at):
        return {"id": vid, "type": "subnode", "config": {
            "node": f"node://{SUBAGENT[agent]}@v{versions[SUBAGENT[agent]]}",
            "input": {"request": "{{input%s.request}}" % at}}}

    spec = _common("Runs the specialist a task names (billing, bug or docs).", "Dispatch one support task.",
                   TASK, {"type": "object"}, model,
                   {"steps": 8, "tokens": 40000, "wall_clock_s": 300, "usd": 0.10, "depth": 2})
    spec.update(kind="workflow", graph={"entry": "is_billing", "vertices": [
        {"id": "is_billing", "type": "verify", "config": {"check": "schema", "schema": is_("billing")}},
        {"id": "is_bug", "type": "verify", "config": {"check": "schema",
                                                      "schema": {"type": "object", "properties": {"previous": is_("bug")}}}},
        sub("billing", "billing", ""),
        sub("bug", "bug", ".previous"),
        sub("docs", "docs", ".previous.previous"),
    ], "edges": [
        {"from": "is_billing", "to": "billing", "when": "pass"},
        {"from": "is_billing", "to": "is_bug", "when": "fail"},
        {"from": "is_bug", "to": "bug", "when": "pass"},
        {"from": "is_bug", "to": "docs", "when": "fail"},
    ]})
    return spec


def support_orchestrator(model, dispatch_version):
    spec = _common("Splits a customer message into billing, bug and docs tasks, runs the specialists in parallel "
                   "and merges their answers.", PLAN_SYSTEM,
                   {"type": "object", "required": ["message"], "additionalProperties": False,
                    "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 4000}}},
                   REPLY, model,
                   {"steps": 24, "tokens": 120000, "wall_clock_s": 600, "usd": 0.50, "depth": 3, "fanout": 3})
    spec.update(kind="workflow", graph={"entry": "plan", "vertices": [
        llm("plan", "Customer message:\n<<<\n{{input.message}}\n>>>", PLAN, max_tokens=600),
        {"id": "fanout", "type": "map", "config": {
            "over": "/tasks", "concurrency": 3,
            "body": {"id": "dispatch", "type": "subnode", "config": {"node": f"node://support-dispatch@v{dispatch_version}"}}}},
        {"id": "reply", "type": "llm", "config": {
            "system": REPLY_SYSTEM, "output_schema": REPLY, "reasoning": "off", "structured_output": "json_schema",
            "max_tokens": 900,
            "prompt": "PLAN_JSON: {{outputs.plan}}\nRESULTS_JSON: {{input}}\nWrite the reply."}},
    ], "edges": [{"from": "plan", "to": "fanout"}, {"from": "fanout", "to": "reply"}]})
    return spec


# ─────────────────────────── golden sets (core docs/evals.md) ───────────────────────────


def _schema_check(schema):
    return {"type": "json_schema", "schema": schema}


GOLDEN = {
    "billing-agent": [
        {"id": "en-double", "input": {"request": "I was charged twice for my March subscription"},
         "checks": [_schema_check(BILLING_OUT), {"type": "contains", "value": ["double_charge"]}]},
        {"id": "fr-invoice", "input": {"request": "Où trouver ma facture de mars avec notre numéro de TVA ?"},
         "checks": [_schema_check(BILLING_OUT), {"type": "contains", "value": ["invoice", "\"fr\""]}]},
        {"id": "ar-refund", "input": {"request": "أريد استرداد قيمة الاشتراك السنوي الذي جُدّد أمس"},
         "checks": [_schema_check(BILLING_OUT), {"type": "contains", "value": ["\"ar\""]}]},
    ],
    "bug-triage": [
        {"id": "en-export", "input": {"request": "The export button crashes the app on our big report"},
         "checks": [_schema_check(BUG_OUT), {"type": "contains", "value": ["export"]}]},
        {"id": "fr-sync", "input": {"request": "La synchronisation du calendrier s'est arrêtée depuis que j'ai changé mon mot de passe"},
         "checks": [_schema_check(BUG_OUT), {"type": "contains", "value": ["sync"]}]},
        {"id": "ar-crash", "input": {"request": "يتعطل التطبيق عند تصدير تقرير كبير"},
         "checks": [_schema_check(BUG_OUT), {"type": "contains", "value": ["\"ar\""]}]},
    ],
    "docs-helper": [
        {"id": "en-csv", "input": {"request": "How do I export a report to CSV?"},
         "checks": [_schema_check(DOCS_OUT), {"type": "contains", "value": ["CSV"]}]},
        {"id": "en-sso", "input": {"request": "How do I set up SAML single sign-on?"},
         "checks": [_schema_check(DOCS_OUT), {"type": "llm_judge", "threshold": 0.6,
                                              "rubric": "Does the answer explain how to set up SAML single sign-on from the documentation?"}]},
        {"id": "fr-export", "input": {"request": "Comment programmer un export hebdomadaire ?"},
         "checks": [_schema_check(DOCS_OUT), {"type": "contains", "value": ["\"fr\""]}]},
    ],
    "support-orchestrator": [
        {"id": "en-two-parts", "input": {"message": "I was double charged this month and the export button crashes the app."},
         "checks": [_schema_check(REPLY), {"type": "contains", "value": ["billing", "bug"]}]},
        {"id": "fr-howto", "input": {"message": "Comment exporter un rapport en CSV ?"},
         "checks": [_schema_check(REPLY), {"type": "contains", "value": ["docs"]}]},
        {"id": "ar-billing", "input": {"message": "تم خصم قيمة الاشتراك مرتين من بطاقتي هذا الشهر"},
         "checks": [_schema_check(REPLY), {"type": "contains", "value": ["billing"]}]},
    ],
}
