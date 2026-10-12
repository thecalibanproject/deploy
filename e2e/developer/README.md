# Developer scenario: intents, then agents, from code

A developer at a SaaS company (fictional product "Acme Boards", tenant `acme-dev`) tries Caliban
out the way developers do: from code. Every step runs a real script of `app/` with the Caliban
Python SDK; the recorded demo (`demo/scenario.py`, contract in `e2e/demo/README.md`) shows each
script's source and output in a terminal card, then the effect in the console. `check.py` asserts
the outcome.

| Step | Script | What happens |
|---|---|---|
| 1 | console, `first_call.py` | An API key minted in the console; a first chat completion with a catalogue model; routed model, intent (`pinned`), cost, request id and timings from the response headers |
| 2 | `intents.py define`, `classify.py first` | Five intents (billing, bug_report, how_to, account_security, sales) with exemplars in English, French and Arabic through the tenant intents API; 15 prompts through `caliban/auto`; a table of intent, confidence, stage, target, client time and the gateway's `Server-Timing` stages, then p50 and p95 |
| 2 | `intents.py teach`, `classify.py taught` | The sales prompts about adding seats miss (sales only knew demos); sales exemplars are added at runtime, the same prompts are classified again, no restart |
| 3 | `agents.py docs`, `publish`, `gate` | Four product docs into a document collection; five node specs written as Python (`support_agents.py`), validated by the SDK and the server, published; the orchestrator's gate refuses while its subagents have no evals |
| 3 | `agents.py evals`, `promote`, `route` | Golden sets and promotion bars, golden evals (pass^k, cost per success, p95) and the injection suite for the agent that reads documents; promotion children first; the support intents (and `multi_issue`) handed to `node/support-orchestrator` |
| 3 | `support.py` | "I was double charged ... and the export button crashes ..." through `caliban/auto`: the orchestrator plans two tasks, runs billing-agent and bug-triage in parallel, merges one reply; the run tree with per-step durations, tokens and cost, and the budget (depth, fan-out) |
| 4 | `usage.py` | Usage per node and per intent (daily routing counts), node spend against the tenant's caps |

## The agents (research note 04)

```
support-orchestrator (workflow)
  plan      llm, constrained to {language, tasks[{agent, request}]}, reasoning off
  fanout    map over the tasks (concurrency 3), each: subnode support-dispatch
              support-dispatch (workflow, no model call): schema checks pick the subagent
                billing-agent  workflow: one llm call constrained to its output schema
                bug-triage     workflow: one llm call (severity, component, known issue)
                docs-helper    agent: builtin://rag_search over product_docs, cited answer
  reply     llm, constrained to {reply, handled_by, language}
```

The plan is fixed before any subagent output exists, so what a subagent returns cannot change
which subagents run (plan then execute; note 04). Model vertices ask for constrained decoding
(`structured_output: json_schema`, vLLM guided decoding) with thinking off (`reasoning: off`,
Qwen3 `enable_thinking`). Every node sets `guardrails.pii: reversible` (note 05: identifiers are
pseudonymised on every model call, sovereign models included, and restored in the answer).
Promotion follows core `docs/evals.md`: each subagent passes its own gate before the version that
pins it can (P3 decision 12). A message with several problems is its own intent, `multi_issue`:
kNN votes split between billing and bug_report would abstain (note 02: agent routing is set-valued).

## Run it

Python 3.11 or later for `app/` (the SDK needs it); seed, check and the scenario helpers are stdlib.
The app scripts need the SDK on their path: the scenario installs it once with
`pip install --target $E2E_WORK_DIR/developer/pylib $DEV_SDK_SRC` (default: `../sdk-python` next to
this repository when it exists, else the GitHub repository's `main`).

**Without Docker** (how it was tested): a `caliban standalone` with the in-memory store, `[rag]
index = "memory"`, a `[routing]` table whose `embedding_model` is served by `mock/model.py`, and
`CALIBAN_KEK` set; `mock/model.py` on a free port (`MOCK_PORT`); then

```sh
export E2E_CP_URL=http://127.0.0.1:18181 E2E_ROUTER_URL=http://127.0.0.1:18180 CALIBAN_ADMIN_TOKEN=...
export DEV_CHAT_BASE_URL=http://127.0.0.1:18100/v1 DEV_EMBED_MODEL=<the routing embedding model id>
python3 e2e/developer/seed.py
# the recording, or the scripts by hand from e2e/developer/app with CALIBAN_BASE_URL, CALIBAN_API_KEY,
# CALIBAN_ADMIN_URL, CALIBAN_ADMIN_TOKEN, DEV_TENANT, DEV_CHAT_MODEL, DEV_STATE set (lib/dev.py app_env)
python3 e2e/developer/check.py
```

**Shared local stack**: `E2E_EXTRA_COMPOSE=$PWD/e2e/developer/docker-compose.dev.yml e2e/health-triage/e2e.sh up`
adds `caliban-e2e-dev-model` (the scripted model) to the stack; then `e2e/demo/run-demo.sh record developer`.

**Testbed** (`E2E_TARGET=testbed`): seed registers `dev/qwen3.8-27b` behind the stack's recorder
(`http://caliban-e2e-model:8000/v1`, upstream `Qwen/Qwen3.8-27B-FP8`, family `qwen3.8`), the
collection embeds with `local/qwen3-embedding-0.6b` and reranks with `local/qwen3-reranker-0.6b`.

| Variable | Default (local / testbed) | Meaning |
|---|---|---|
| `DEV_TENANT` | `acme-dev` | The tenant |
| `DEV_CHAT_MODEL` | `dev/chat` / `dev/qwen3.8-27b` | Catalogue id of the developer's chat model |
| `DEV_CHAT_BASE_URL` | `http://caliban-e2e-dev-model:8000/v1` / `http://caliban-e2e-model:8000/v1` | Where that model is served |
| `DEV_UPSTREAM_MODEL`, `DEV_MODEL_FAMILY` | `dev-scripted`, none / `Qwen/Qwen3.8-27B-FP8`, `qwen3.8` | The upstream name and family (Qwen3: thinking switched per call) |
| `DEV_EMBED_MODEL`, `DEV_RERANK_MODEL` | `E2E_ROUTING_EMBED_MODEL`, none / Qwen3 embedding and reranker | The document collection's models |
| `DEV_SDK_SRC` | `../sdk-python` or the GitHub `main` | What `pip` installs the SDK from |
| `DEV_EVAL_K`, `DEV_MIN_PASS_K`, `DEV_MAX_P95_MS` | `3`, `0.66`, `60000` / `300000` | Golden evals and the promotion bar |
| `DEV_INTENT_SETTLE_S` | `5` | Wait after an intent change (see gaps) |
| `DEV_MIN_ROUTED_OK` | `14` | check.py: prompts that must route as expected after the fix |

## Gaps found by this scenario

- **kNN picks up exemplar changes on the request after the change.** Routers rebuild the index in
  the background when a request arrives after the snapshot changed, and that request is routed with
  the old index (`crates/caliban-gateway/src/route_embed.rs`, `spawn_refresh_if_needed`; documented
  on `Gateway::warm_router`). The scripts send one small `caliban/auto` request after each change,
  then wait. A snapshot apply could start the rebuild, and the intents API could say when routers
  serve the new exemplars.
- **The console shows the run tree flat.** Subnode and `node://` steps run inside the parent run
  (no child runs); the nesting is only in the step ids (`fanout#0/1>bug#0>triage#0`). The run
  page's Steps table prints the ids as they are; `support.py` draws the tree. Map branches and
  subnode vertices have no step of their own, so a branch's duration is derived from its steps.
- **The Python SDK (main) does not wrap**: publish, promote, versions, routes, `node_routes` and
  `node_spend_caps` on tenants (its tenant models forbid the fields), console runs, node-spend,
  usage filters and `by_node` / `by_route` (untyped extras). `app/common.py raw()` calls them with
  the SDK's own HTTP client. `CalibanResponseMeta` does not carry `Server-Timing`; `Timed` reads it
  with an httpx event hook.
- **No node-spend view in the console** (the Usage page has no current spend against the caps).
- **Agent tools from `node://` refs get a generic description and `{"type": "object"}` parameters**,
  so a model choosing among them sees little; with note 04's preference for explicit graphs, that is
  why the orchestrator is a plan plus a map over a dispatch workflow rather than an agent with three
  node tools.
- **No mock MCP server in the e2e stack**, so bug-triage has no tools (a known-issue list in its
  prompt instead of an issue-tracker lookup).
