# Running the health triage e2e on the AWS testbed

The same test as locally, against the production model tier of research note 09 on one L40S
48 GB: Qwen3.8-27B-FP8 on vLLM 0.31.0 for chat, Qwen3-Embedding-0.6B (TEI) for `caliban/auto`'s
kNN, Qwen3-Reranker-0.6B started with them. The GPU host serves the models (compose profiles
`qwen3-large`, `embeddings`, `reranker`); the gateway host runs Caliban's compose stack, and the
e2e adds MongoDB and the model recorder to it. Testbed rules apply (aws/testbed/README.md): the
shared account's prefix, confirm before apply, always destroy.

## 1. Bring the testbed up

The e2e directory must be in the deploy tree the gateway clones, so push the branch first and
point `deploy_ref` at it. The gateway's Caliban image must come from core `ae553e6` or later: setup
uses introspection, ontology proposals and review batches, tenant intents, the tenant's PII
settings and the node spec fields core added there (README, "Gaps in core"), and an older image
refuses them. In build mode `core_ref` (default `main`) must point at such a commit on GitHub; in
s3 mode the bundle must hold such an image.

```sh
cd aws/testbed
tofu apply -var gpu_enabled=true -var deploy_ref=e2e-core-gaps   # plus your usual -var-file
tofu output ssm_sessions
```

`gpu_compose_profiles` defaults to `["qwen3-large", "embeddings", "reranker"]`; the gateway's
`caliban.toml` then points the model providers at the GPU host (8000 chat, 8001 embed, 8002
rerank). The gateway needs to pull `mongo:8.0.32` and `python:3.11-slim` once (public or NAT
scenario; for the zero-egress scenario, add both images to the bundle).

## 2. Check the models (GPU host, SSM session)

```sh
sudo -i
cd "$CALIBAN_DEPLOY_DIR/compose" && docker compose ps            # qwen3-large, embed, rerank healthy
curl -s localhost:8000/v1/models | head -c 300; echo               # Qwen/Qwen3.8-27B-FP8
```

## 3. Run the test (gateway host, SSM session)

```sh
sudo -i
cd "$CALIBAN_DEPLOY_DIR"
git log -1 --oneline                                               # the deploy_ref commit
export E2E_TARGET=testbed                                          # GPU_IP comes from /etc/profile.d
e2e/health-triage/e2e.sh up        # e2e services, caliban recreated with the e2e config, seed
e2e/health-triage/e2e.sh setup     # tenant, datasource, ontology, node, evals (minutes), gate, intent
e2e/health-triage/e2e.sh run       # the seven requests; prints every check
cat e2e/health-triage/.work/report.md
```

What `E2E_TARGET=testbed` sets (override any of them in the environment):

| Setting | Value |
|---|---|
| Compose | project `caliban`, `compose/docker-compose.yml` + `/opt/caliban-testbed/compose.testbed-gateway.yml` + the e2e overlay, env file `compose/.env` |
| Config | `/opt/caliban-testbed/caliban.toml` plus the e2e `[routing]` table (Qwen3-Embedding instruction prefix and thresholds from core's `open-models.example.toml`); the tenant's `triage` exemplars are set by `setup` through the admin API |
| Chat model | `e2e/chat` = `Qwen/Qwen3.8-27B-FP8`, family `qwen3.8`, through the recorder (`caliban-e2e-model`) to `http://$GPU_IP:8000/v1`, registered `t0_sovereign`; the node's `guardrails.pii: mask` masks identifiers on its calls |
| Routing embedder | `local/qwen3-embedding-0.6b` (the stack's TEI on the GPU host) |
| Node calls | thinking off on `assess` and `recommend` (`E2E_ASSESS_REASONING`, `E2E_RECOMMEND_REASONING`: `off`, `low`, `medium`, `high`; the gateway sends Qwen3.8 `chat_template_kwargs.enable_thinking`), JSON outputs with `response_format: json_schema` (vLLM guided decoding) |
| Evals | golden p95 bar 300 s, eval timeout 1 h: room for a run with thinking on |
| RAG stage (`E2E_RAG=1`) | collection `care_docs` embedded with `local/qwen3-embedding-0.6b` and reranked with `local/qwen3-reranker-0.6b` (the GPU host's TEI on 8001 and vLLM on 8002), vectors in the gateway's Qdrant |

Expect each triage request to take seconds with thinking off, tens of seconds with it on (two or
three model calls); `CALIBAN_NODE_SYNC_WAIT_SECS` is 180 in the e2e, and the run script follows a run that outlives it. The scripted human answers
and the fallback answers cover a real model that asks a question where the scripted one does not;
quality checks (specialty, city, facility) are reported, and fail the run only with
`E2E_STRICT_QUALITY=1`.

Optional, before `up`:

- NER: with a verified artifact in the gateway's `compose/models/pii` and `CALIBAN_PII_NER_DIR` set
  in `compose/.env`, run with `E2E_EXPECT_NER=1` so names must be masked.
- Negative control for the injection row: `E2E_FOLLOW_INJECTIONS=1` only affects the scripted
  model, not a real one.

### The RAG stage

Optional, off by default. It needs a gateway image with core's retrieval (the gateway's
`caliban.toml` carries the `[rag]` table, which an older image refuses) and Qdrant 1.19.2 (the
compose default). Run it with the triage stages, or add it to a stack already set up:

```sh
export E2E_TARGET=testbed E2E_RAG=1
e2e/health-triage/e2e.sh setup     # the triage setup again (idempotent), then the collection,
                                   # the eight documents, indexing, node careguide and its key
e2e/health-triage/e2e.sh run       # R1 to R7, then G1 to G4; `run G1 G3` for some of them
```

Indexing goes through the real embedder: eight short documents take seconds, not minutes. With a
real model the agent may phrase its search differently and pick other passages: the `rag` checks
(retrieval, quarantine, taint, grounded citations, metering) must hold whatever it does; the
`quality` checks (best passage, `[n]` markers, language, the expected fact) are reported and fail
the run only with `E2E_STRICT_QUALITY=1`. The `careguide` agent's JSON answer still relies on
prompting plus one corrective retry: core's constrained decoding covers the model vertices of
workflows, not an agent's final answer (README, "Gaps in core").

With `gateway_run_worker=true` the worker on the gateway indexes too, as the gateway's own database
user: the worker grants of the chart README (`rag_collection`, `rag_document`) are not exercised
here.

Keep the report: copy `e2e/health-triage/.work/report-*.md` and `results-*.json` to the testbed
bucket's `results/` prefix (with `results_upload = true`) or paste them from the session.

## 3b. The recorded product demo (optional)

`e2e/demo/run-demo.sh` records the product the way a customer sees it (e2e/demo/README.md): the
gateway host in a terminal, then an operator who sets the scenario up in the web console, the
client requests, what the console shows of each, the checks, and the timing telemetry. For health
triage the console walkthrough replaces `e2e.sh setup`: it writes the same `.work/state.json`, and
the recorded check is `e2e.sh run` (lib/run.py), which must pass exactly as after the API setup.
That is the proof that the console setup is equivalent.

Run it on a fresh tenant: right after the first `e2e.sh up`, before any `e2e.sh setup` (a stack set
up through the API shows its existing objects instead of creating them). `run-demo.sh all` records
`e2e.sh up` itself, which is quick when the stack is already up.

```sh
sudo -i
cd "$CALIBAN_DEPLOY_DIR"
export E2E_TARGET=testbed                    # E2E_RAG=1 adds the retrieval chapter and G1 to G4
e2e/health-triage/e2e.sh up                  # once, if not done yet
e2e/demo/run-demo.sh all                     # or: e2e/demo/run-demo.sh health-triage
ls e2e/health-triage/.work/demo/             # caliban-demo.mp4, health-triage.mp4, telemetry.md, ...
DEMO_UPLOAD=1 e2e/demo/run-demo.sh upload    # copy to s3://$CALIBAN_TESTBED_BUCKET/results/demo-<time>/
```

Single steps: `run-demo.sh pull | fonts | infra | seed | record | check | cards | stitch | upload`,
the last five with scenario names. `DEMO_PACE` (default 1) scales the viewer pauses,
`DEMO_SLOWMO_MS` (120) slows every browser action, `DEMO_MAX_IDLE_S` (10) is the longest wait the
stitch keeps whole (evals, indexing).

The gateway pulls three more images (`run-demo.sh pull`; for the zero-egress scenario, add them to
the bundle):

| Image | Use |
|---|---|
| `mcr.microsoft.com/playwright/python:v1.63.0-noble` | Chromium and Python Playwright: the console recordings and the cards |
| `ghcr.io/asciinema/agg:1.9.0` | The terminal recordings, asciicast to GIF (amd64 only: on the arm64 gateway, `DEMO_AGG=<path>` to the release's `agg-aarch64-unknown-linux-gnu`, sha256 `2b4be407b97e00e1c313a41d154ced8fa3d02c560c8f47a0db4950a2576444c9`, and the image is not pulled) |
| `jrottenberg/ffmpeg:8.0.3-ubuntu2404` | MP4 encoding, cuts, concatenation, chapter markers |

Outputs, in `e2e/health-triage/.work/demo/`: `caliban-demo.mp4` (everything, with chapter
markers), `<scenario>.mp4`, `<scenario>.webm` and `<scenario>.timeline.json` (chapters and long
waits in seconds, and the steps done through the admin API because the console has no page for
them), `shots/<scenario>/`, `term/*.cast` (replayable) and `term/*.mp4`, `<scenario>-telemetry.jsonl`,
`telemetry.md` and `telemetry.csv`. With `results_upload = true`, `upload` copies them (not the
intermediate parts) with the e2e report to the bucket's `results/` prefix.

The browser runs in the Playwright container on the host network and reaches the console and the
data plane at `E2E_CP_URL` and `E2E_ROUTER_URL`; it gets the e2e's environment except the MongoDB
root credentials and key file. The key the console mints is blurred in the recording, the admin
token and the MongoDB password are typed into password fields.

## 4. Clean up

```sh
e2e/health-triage/e2e.sh down      # e2e containers, volumes and network; caliban back on its own config
```

Then destroy the testbed (`tofu destroy`, same variables). `down` leaves the tenant `medmarket`
and its nodes in the gateway's Postgres, and the RAG stage's vectors in its Qdrant
(`caliban_rag_*` collections); they go with the testbed.
