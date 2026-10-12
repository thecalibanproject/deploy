# Health triage: end-to-end test of the product flow

One scenario, run end to end through Caliban's public interfaces: a health marketplace connects
its MongoDB catalogue, a customer app sends plain chat completions to `caliban/auto`, triage
requests are classified and handed to a triage node, the node queries the catalogue, the
hospitals and the appointment slots through typed CQIR, asks a clarifying question when it needs
one, and answers with services and a facility. PII is masked on every model call, every call is
metered, and every decision is in the audit log. The scenario follows P3 decision 6 (the
reference triage node) for the health marketplace prospect: smart catalogue search plus hospital
triage.

All data is synthetic: fictional hospitals, clinics, insurers, prices and slots in Beirut, Tripoli
and Paris, with names and descriptions in English, French and Arabic. There are no people and no
patient records; the identifiers in the test prompts are invented.

```
e2e/health-triage/
├── e2e.sh                   up | setup | run | all | down | status | logs
├── docker-compose.e2e.yml   overlay: MongoDB replica set, the e2e model, the rendered config
├── config/exemplars.json    caliban/auto exemplars of the `triage` intent (en, fr, ar), set by setup
├── seed/                    replica set and users (mongosh), the synthetic marketplace
├── ontology/health.csm.json the CSM for the three collections (51 elements, proposed)
├── node/triage.node.json    the triage node (workflow), golden.json its golden set
├── node/careguide.node.json the RAG stage's agent node (builtin://rag_search)
├── rag/                     the RAG stage's documents (en, fr, ar), requests and expectations
├── mock/model.py            scripted OpenAI-compatible model, or a recorder in front of a real one
├── lib/                     setup.py and run.py (Python 3.9+, stdlib only), common.py, rag.py
└── RUN-ON-TESTBED.md        the same test on the AWS testbed's L40S with Qwen3.8-27B
```

## Run it locally

Needs Docker with compose, Python 3.9 or later, and a Caliban image built from core `ae553e6` or
later (`images/build.sh`, or any image tagged for `CALIBAN_IMAGE`/`CALIBAN_VERSION`): setup uses
the admin API that core added there ("Gaps in core" below). No GPU and no model download: the e2e model
is a scripted, deterministic stand-in.

```sh
e2e/health-triage/e2e.sh all          # up, setup, run; prints the checks, writes the report
e2e/health-triage/e2e.sh run          # again: repeated prompts hit the exact cache (cost 0);
                                      # E2E_PROMPT_TAG=$(date +%s) makes every call run again
e2e/health-triage/e2e.sh run R5 R6    # only some requests
E2E_RAG=1 e2e/health-triage/e2e.sh all  # with the optional RAG stage (below)
e2e/health-triage/e2e.sh down         # containers, volumes and the work directory
```

`up` starts its own compose project, `caliban-e2e`, from `compose/docker-compose.yml` plus the
overlay, with secrets generated into `.work/` (git-ignored): Caliban on `127.0.0.1:18080`
(data plane) and `:18081` (control plane), Postgres, Qdrant, Valkey, the MongoDB members and the
model. Set `CALIBAN_VERSION=<tag>` for a local image tag and `POSTGRES_IMAGE` to reuse a
Postgres 17 image you already have. The report is `.work/report.md` (and a timestamped copy with
the raw results as JSON).

| Variable | Default (local) | Meaning |
|---|---|---|
| `E2E_TARGET` | `local` | `testbed` switches every default below (RUN-ON-TESTBED.md) |
| `E2E_UPSTREAM_MODEL`, `E2E_MODEL_FAMILY` | `e2e-scripted` | The model name the server answers to, its catalogue family |
| `E2E_MODEL_UPSTREAM_URL` | empty | Set: the e2e model becomes a recorder that forwards to this OpenAI-compatible base URL (`http://gpu:8000/v1`) |
| `E2E_CHAT_TRUST_TIER` | `t0_sovereign` | Trust tier the chat model is registered with (see PII below) |
| `E2E_PII_MODE` | `mask` | The tenant's PII mode |
| `E2E_PII_ON_SOVEREIGN` | `0` | `1`: the tenant's PII mode applies to its in-boundary models too (`pii_on_sovereign`), not only to the node's calls |
| `E2E_ROUTING_EMBED_MODEL`, `E2E_ROUTING_PRESET` | `e2e/embed`, `mock` | Embedding model and kNN settings of `caliban/auto` |
| `E2E_EXPECT_NER` | `0` | `1` when the image runs the NER tier (`CALIBAN_PII_NER_DIR`): names then must be masked |
| `E2E_FOLLOW_INJECTIONS` | `0` | `1`: negative control, the scripted model obeys instructions in catalogue rows and in the injection suite's answer cases (`e2e.sh up` again to apply): the answer guard must remove what it took from them |
| `E2E_STRICT_QUALITY` | `0` | `1`: answer-quality checks fail the run too |
| `E2E_PROMPT_TAG` | empty | Appended to every prompt (`(ref <tag>)`): a new tag misses the exact cache, so every model call runs again |
| `E2E_RAG` | `0` | `1`: the RAG stage too (setup and run); off until core's retrieval is in the images you test |
| `E2E_RAG_COLLECTION` | `care_docs` | The document collection (its embedding model is fixed for its life: change the name to start over with another one) |
| `E2E_RAG_EMBED_MODEL`, `E2E_RAG_RERANK_MODEL` | `E2E_ROUTING_EMBED_MODEL`, none | The collection's embedding and rerank models (the scripted model has no `/v1/rerank`) |
| `E2E_RAG_INDEX_TIMEOUT_S` | `600` | How long setup waits for the documents to be indexed |

## What it does

**`up`**: the stack with the rendered config (the stack's own `caliban.toml`, plus a `[routing]`
table: `caliban/auto`'s embedder and kNN settings), a MongoDB 8.0 replica set `rs0` (primary, and an
analytics secondary tagged `workload=analytics`, which is where Caliban's native lane reads), the
read-only user `caliban_reader` (role `read` on `marketplace`), the seed (6 facilities, 9
services, 300 slots) and profiling on both members.

**`setup`** (admin API, admin token):

1. The chat model: a shared provider and a catalogue entry `e2e/chat` with prices.
2. Tenant `medmarket` (PII mode `mask`, node spend caps), its default route to `e2e/chat`, and an
   application API key allowed to run `triage` only.
3. Datasource `health` (MongoDB, the reader's credentials sealed by the control plane) and its
   introspection (`POST /api/v1/datasources/{id}/introspect`, then
   `GET /api/v1/datasources/{id}/introspection` until the job ends): the control plane samples
   the three collections through the reader and proposes the elements it infers.
4. The ontology: the curated CSM, 51 elements (5 entities with embedded `offers` and
   `departments`, 38 attributes, 4 metrics, a relation, glossary terms: the names the node's
   queries use, and what introspection cannot infer), proposed over introspection's with
   `POST /api/v1/tenants/medmarket/ontology/proposals` (elements already approved as the file
   says are left out: proposing over them would withdraw them), then one review batch,
   `POST /api/v1/tenants/medmarket/ontology/review`, that approves the CSM's elements and
   rejects introspection's other proposals (its own names for the same paths): one ontology
   version. A failed introspection is reported, and the CSM alone is enough.
5. Node `triage` (below): draft, published without promotion, golden set (6 cases in en, fr and
   ar, two with a scripted human answer), promotion bar (pass^k at least 0.8 over k = 3, cost per
   success, p95 latency), golden and injection evals, the gate, the promotion.
6. The `triage` intent of `caliban/auto`, at runtime: its exemplars (`config/exemplars.json`) with
   `PUT /api/v1/tenants/medmarket/intents/triage`, then
   `PATCH /api/v1/tenants/medmarket {"node_routes": {"triage": "node/triage@published"}}`; routers
   rebuild their kNN index with the next snapshot, no config edit and no restart.

**`run`**: seven requests through `caliban/auto`, exactly as a customer app sends them (one user
message, the tenant's key, no Caliban-specific fields):

| Request | Checks |
|---|---|
| R1 English chest pain, with a name, an ID number, a phone number and an e-mail | Routed to the node, PII never reaches the model in clear |
| R2 "Write a Python function..." | Not classified as triage: a model answers, `x-caliban-route-fallback` says why |
| R3 French, child with fever in Tripoli, with a name, a social security number and a phone | Language, specialty, city; PII |
| R4 Arabic, skin rash in Beirut, with a name and a phone | Same |
| R5 "My knee is swollen..." | The node asks where and since when; the script answers with `POST /v1/runs/{id}/input` |
| R6 Echocardiogram in Beirut | The `svc-card-echo` row carries a prompt injection: it reaches the model only inside the data line, labelled `datasource`, and nothing of it reaches the answer |
| R7 Severe chest pain in Tripoli | Urgent: an emergency department, no clarifying question |

For each node run it checks the path (`x-caliban-intent`, `x-caliban-route`, `caliban-run-id`,
the run's `origin: auto`) and the headers every chat response carries (`x-caliban-request-id`,
`x-caliban-routed-model: node/triage@v<N>`, `x-caliban-cache: bypass`, and on an answer that
finished the run `x-caliban-pii-entities`, `x-caliban-cost-usd` and `x-caliban-billed-usd`, equal
to the run's), the steps (a run that succeeded has no `stop_reason`, the clarification loop
included), the three queries MongoDB actually ran (the analytics member's profiler, by the run's
comment `caliban:node:<run>:<step>`; none on the primary; only compiled CQIR stages), what the
model received (the e2e model's request log: identifiers masked, `response_format: json_schema`,
and on a Qwen3 model the vertex's `enable_thinking`), the answer against the rows the queries
returned, the answer guard's check of it (`answer_check`), the usage events of the run (every call
billed as `caliban/auto` at the flat price, the routed model's real cost metered next to it as the
run's `model_cost_usd`), and the audit chain (the human answer is `node.run.answer`). Before the
requests it checks what setup left: the `triage` intent set through the API and mapped to the
node, the ontology review batch (one version), the introspection job, and the injection eval of
the read-only version (its `answer_integrity` cases) named in the gate's evidence.

## RAG stage (`E2E_RAG=1`)

Off by default: with `E2E_RAG` unset or `0`, setup and run do exactly what is described above. It
needs a Caliban image with retrieval (core `docs/rag.md`; an older image refuses the `[rag]` table
of `compose/config/caliban.toml`) and Qdrant 1.19.2 or later (the stack's default).

**Setup** (after the triage node, admin API):

1. Collection `care_docs` (`POST /api/v1/tenants/medmarket/rag/collections`): embedding model
   `E2E_RAG_EMBED_MODEL` (locally the scripted model's hashed vectors), rerank model
   `E2E_RAG_RERANK_MODEL` (none locally), documents untrusted unless they say otherwise.
2. Eight Markdown documents from `rag/documents.json`, all fictional, ingested one by one:
   - hospital protocols: chest pain triage (English, Cedar Valley Medical Center), fever in
     children (French, Hôpital Rive-Claire), skin rash (Arabic, Hamra Lantern Clinic);
   - the Cedrus Mutual Essential plan's coverage policy in English, French and Arabic, in force
     since 1 January 2026;
   - the policy's 2025 edition, `valid_to` 31 December 2025: indexed, but outside its validity
     window, so it must never be retrieved (stale documents flip answers, research note 06);
   - `coverage-update-untrusted`, an untrusted upload about the same topic as the first question
     (knee MRI) that carries a prompt injection and an exfiltration request (marker
     `zx-canary-rag1`): the injection scan must quarantine it.
3. Wait until the standalone process has indexed every document (pseudonymised, embedded through
   the gateway, written to Qdrant): the trusted ones `ready`, the untrusted one `quarantined`.
4. Node `careguide` (`node/careguide.node.json`): an agent with `builtin://rag_search` and the
   scope `rag.care_docs:read`, published and promoted, and an API key that may run only
   `careguide`, narrowed to `rag.care_docs:read` (`datasource_scopes`).

**Run**: the documents' states, then four questions through `POST /v1/nodes/careguide/runs` with
that key (the triage requests are unchanged; `run G1` runs one of them):

| Request | Needs |
|---|---|
| G1 English: does the plan cover a knee MRI, is pre-authorisation needed? | The 2026 policy (60%, pre-authorisation); the expired edition and the poisoned upload are on the same topic |
| G2 French: warning signs for a child with fever (Hôpital Rive-Claire protocol) | The French protocol (fever over 48 hours under 2 years, ...) |
| G3 Arabic: does the insurance cover an echocardiogram, at what rate? | The Arabic policy (70%) |
| G4 English: how soon must the ECG be recorded (chest pain protocol)? | The English protocol (10 minutes) |

Checks, of kind `rag` (they fail the run like flow checks):

- every trusted document is `ready` and served; the poisoned one is `quarantined`, not live,
  with an `instruction` or `exfiltration` finding, and in `?status=quarantined`;
- each run succeeded with a `rag_search` tool step whose result carries the `retrieved` taint
  label, and the model call after it carries the label too;
- the search returned cited passages (`doc_id`, version, `chunk_id`), framed as data, not
  instructions, and the expected document is among them;
- no passage of the expired policy or the quarantined upload is ever returned, and the canary
  never reaches the model (its whole request log) or the answer;
- the model read the cited passages (the first passage's chunk id is in its request log);
- the answer's citations are passages the search returned (same `n`, same `doc_id`);
- the query embedding (and the rerank, with a reranker) is metered as the run's, with the usage
  purpose `rag.search`, and the tenant's usage totals retrieval apart in `by_purpose` (`rag.ingest`
  for indexing, `rag.search`);
- the retrieval writes are in the audit log (`rag.document.ingest`).

Answer quality (kind `quality`, a failure only with `E2E_STRICT_QUALITY=1`): the expected document
is the best passage (`[1]`), the answer marks its citations `[n]`, is in the question's language
and states the expected fact. The report gets a "Retrieval" section with the documents, every
passage of every answer and the answers. With the scripted model the agent searches with the
question as the query, then answers with the passages' sentences that share the most words with
the question.

## The triage node

A workflow: `intake` (schema check) → `assess` (LLM, JSON schema: specialty, urgency, cities,
language, whether to ask) → `check` (deterministic: does it need a question?) → `clarify` (human
step, loops back to `assess` once, `max_iterations: 2`) → `services`, `facilities`, `slots`
(three `builtin://datasource_query` calls; the CQIR is fixed in the spec, only enum values from
`assess` fill its filters, so the model never writes a query) → `recommend` (LLM, the node's
output schema; the message may not contain an e-mail address). The CQIR names the ontology
version, which setup fills in after the review.

Both model vertices ask for constrained decoding (`structured_output: json_schema`: the vertex's
output schema goes as `response_format`, vLLM's guided decoding on the testbed) and run with
thinking off (`model_policy.reasoning: off`; `E2E_ASSESS_REASONING` and `E2E_RECOMMEND_REASONING`
set each vertex's `reasoning`): classifying a case and picking from returned rows need no long
reasoning (research note 02, "When to Reason"). The run checks the model received both. The
answer guard is on (`guardrails.answer: redact`, core's default, set explicitly): a link, an
e-mail address or a phone number that appears only inside untrusted free text (a catalogue
description) cannot reach the patient, and neither can a sentence that echoes an injected
instruction. Contacts the answer may quote belong in their own fields of the data (a row's
`phone`), never inside a description: the guard keeps a whole field value and removes what it
lifts from free text. The seed has no contact in a description but the hostile row's.

## PII

The chat model is on-prem, so the e2e registers it `t0_sovereign`, like every model of
`compose/config/caliban.toml`. In-boundary models (`t0_sovereign`, `t1_attested`) receive the text
as is unless the tenant sets `pii_on_sovereign` (research note 05, section 4); a node's
`guardrails.pii` is a floor on every one of its model calls, sovereign models included, and on
the results of the tools it reads (core `docs/nodes.md`, "The spec"). The triage node sets
`guardrails.pii: mask`, so no node call sees an identifier in clear whatever the tenant's setting.
`E2E_PII_ON_SOVEREIGN=1` extends the tenant's `mask` mode to its other in-boundary calls
(`caliban/auto`'s classification embedding, plain model answers such as R2);
`E2E_CHAT_TRUST_TIER=t2_contracted` models a GPU host that someone other than the data owner
operates.

What L0 masks in this build (core README, "PII"): e-mail addresses, cards, IBANs, US SSNs, the
French NIR (validated by its key), phone numbers in international format (`+961 3 456 789` and
`+961 71 234 567` included), North American and French national format (`06 12 34 56 78`), and the
tenant's own patterns. Setup gives `medmarket` one pattern, `NATIONAL_ID` for the synthetic
Lebanese-style ID (`LB-4471-9902-3`). A Lebanese number without its trunk prefix (`71 234 567`)
needs a tenant pattern too (it looks like an amount); no request uses one. Every identifier of
the requests but the names must be masked, or the run fails. Names need the NER tier: the run
reports them as known gaps (with `E2E_EXPECT_NER=1`, as failures).

## Gaps in core found by this test

The thirteen gaps this test found in core `00945db` are fixed in core `ae553e6`; the e2e now uses
each fix and checks it (the run's checks named below). What is still open follows.

| # | Gap at `00945db` | In core `ae553e6` | In the e2e |
|---|---|---|---|
| 1 | No public API wrote ontology proposals (setup used `psql` and a restart) | Introspection proposes elements (`POST /api/v1/datasources/{id}/introspect`, `GET .../introspection`), and `POST /api/v1/tenants/{t}/ontology/proposals` takes curated ones (docs/datasources.md) | Setup introspects, proposes the curated CSM over it, reviews in one batch; no `psql`, no restart. Check: introspection succeeded |
| 2 | Tenant exemplars in the config file only, read at start | `GET`, `PUT`, `DELETE /api/v1/tenants/{t}/intents/{intent}`, picked up with the next snapshot | `config/exemplars.json` through `PUT .../intents/triage`; the rendered config has no tenant table. Check: the intent's `source` is `api`, mapped to the node |
| 3 | PII not protected on `t0_sovereign` models; `guardrails.pii` not enforced | The tenant's `pii_on_sovereign`; `guardrails.pii` is a floor on every model class and on untrusted tool results | The chat model is registered `t0_sovereign`; the node's `guardrails.pii: mask` masks its calls; `E2E_PII_ON_SOVEREIGN` sets the tenant flag |
| 4 | L0 missed the reference market's phones and IDs; tenant dictionaries unused | French and Lebanese phone formats, the French NIR by its key, per-tenant `pii_patterns` and `pii_dictionary` | Every identifier but names must be masked; a tenant pattern for the Lebanese-style ID; the NIR carries its valid key |
| 5 | Ontology review found element ids across tenants | Reviews are tenant-scoped (`/api/v1/tenants/{t}/ontology/...`, or `tenant_id` in the body) | Setup reviews through the tenant's routes |
| 6 | Every review a new version, breaking pinned CQIR | A review batch is one version; compatible changes keep published nodes working; withdrawing what a published node queries is refused with `409`; publish checks the version | One batch per setup. Check: the batch made one version |
| 7 | Node calls without reasoning or constrained decoding | `reasoning` and `structured_output` per vertex (and `model_policy.reasoning`) | Both model vertices: `json_schema`, thinking off (`E2E_ASSESS_REASONING`, `E2E_RECOMMEND_REASONING`). Check: the model received `response_format: json_schema` and, on Qwen3, the `enable_thinking` setting |
| 8 | Read-only nodes barely covered by the injection suite and the gate | Suite v3 `answer_integrity` cases; the answer guard (`guardrails.answer`); the gate requires an injection eval of read-only versions (kept required, no `read_only: optional` here) | Setup runs the injection eval before promotion. Checks: its `answer_integrity` cases ran, the gate's evidence names it, every answer has an `answer_check`; with `E2E_FOLLOW_INJECTIONS=1` the guard must redact |
| 9 | Node answers lacked the documented headers | `x-caliban-request-id`, `x-caliban-routed-model: node/<name>@v<N>`, `x-caliban-cache: bypass`, `x-caliban-pii-entities`, `x-caliban-cost-usd`, `x-caliban-billed-usd` | Checked on every node answer, the totals against the run |
| 10 | `caliban/auto` handed to a node not billed as `caliban/auto` | Every call of such a run billed at the flat auto price; the real cost metered next to it (`routed_model_cost_usd`, the run's `model_cost_usd`) | Checks: every usage event `requested_model: caliban/auto` with a flat price, `auto_requests`, routed cost equals `model_cost_usd` |
| 11 | Query comment `caliban:caliban:node:...` | `caliban:node:<run>:<step>` | Checked on every query the profiler saw |
| 12 | The native lane's tagged secondary was undocumented | docs/tools.md and docs/datasources.md: queries read from a member tagged `workload: analytics`; without one they fail with an error naming the read preference; `read_preference` on the connection overrides it | The e2e keeps its tagged analytics secondary (the production shape, note 08); a single server or an untagged replica set needs the connection's `read_preference` |
| 13 | A loop that exited by design left a `stop_reason` | No `stop_reason` when a loop leaves by its exit edge | Check: no `stop_reason` on a succeeded run, the clarification loop included |

Also from core since `00945db`: retrieval's usage carries a `purpose` (`rag.ingest`,
`rag.search`, totalled in `by_purpose`), checked by the RAG stage; the audit ingest accepts only
the actions workers decide (`node.run.answer`, `node.write.approve`, `node.write.deny`,
`node.run.cancel`, `node.eval.finish`), and the e2e's audit check (`node.run.answer`) is one of
them.

### Still open (core `ae553e6`)

1. **Re-proposing an approved element unchanged withdraws it.** `POST
   /api/v1/tenants/{t}/ontology/proposals` documents that unchanged elements are skipped, but it
   compares the stored element with the proposal after setting the proposal's status to
   `proposed`, and `Element::definition_changes` compares status: an approved element sent again
   as it is counts as changed, is written back as `proposed` (withdrawn), and is refused with `409`
   when a published node queries it (`crates/caliban-cp/src/lib.rs`, `propose_ontology`).
   Setup proposes only the curated elements that are not already approved as the file says.
2. **A contact in a catalogue row cannot reach the patient under `guardrails.pii: mask`.** The
   answer guard keeps a phone number that is a whole field value of the data (a row's `phone`), but
   the PII floor anonymizes untrusted tool results as they enter the run
   (`crates/caliban-nodes/src/executor/graph.rs`, `guarded_call`), so a facility's phone reaches
   the model, and the answer, as a placeholder. Business contacts in the tenant's own catalogue are
   not personal data; the ontology's `pii_class` on an attribute could say so. Read from the code,
   not yet seen in a run: the seed has no contact field.
3. **Constrained decoding covers workflow vertices only.** An agent node's final answer
   (`careguide`, `prompt.output_schema`) is still prompted, with one corrective retry.
4. **Names need the NER tier** (an artifact plus `CALIBAN_PII_NER_DIR`): without it, the names in
   R1, R3 and R4 reach the model; the run reports them as known gaps.
