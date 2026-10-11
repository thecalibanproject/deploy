# Running the health triage e2e on the AWS testbed

The same test as locally, against the production model tier of research note 09 on one L40S
48 GB: Qwen3.8-27B-FP8 on vLLM 0.31.0 for chat, Qwen3-Embedding-0.6B (TEI) for `caliban/auto`'s
kNN, Qwen3-Reranker-0.6B started with them. The GPU host serves the models (compose profiles
`qwen3-large`, `embeddings`, `reranker`); the gateway host runs Caliban's compose stack, and the
e2e adds MongoDB and the model recorder to it. Testbed rules apply (aws/testbed/README.md): the
shared account's prefix, confirm before apply, always destroy.

## 1. Bring the testbed up

The e2e directory must be in the deploy tree the gateway clones, so push the branch first and
point `deploy_ref` at it.

```sh
cd aws/testbed
tofu apply -var gpu_enabled=true -var deploy_ref=e2e-health-triage   # plus your usual -var-file
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
git log -1 --oneline                                               # the e2e-health-triage commit
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
| Config | `/opt/caliban-testbed/caliban.toml` plus the e2e `[routing]` table (Qwen3-Embedding instruction prefix and thresholds from core's `open-models.example.toml`) and the tenant's exemplars |
| Chat model | `e2e/chat` = `Qwen/Qwen3.8-27B-FP8`, family `qwen3.8`, through the recorder (`caliban-e2e-model`) to `http://$GPU_IP:8000/v1`, registered `t2_contracted` so the gateway masks PII |
| Routing embedder | `local/qwen3-embedding-0.6b` (the stack's TEI on the GPU host) |
| Evals | golden p95 bar 300 s, eval timeout 1 h: Qwen3.8 thinks before every node call, core sends no reasoning setting with them |

Expect each triage request to take tens of seconds (two or three thinking calls); `CALIBAN_NODE_SYNC_WAIT_SECS`
is 180 in the e2e, and the run script follows a run that outlives it. The scripted human answers
and the fallback answers cover a real model that asks a question where the scripted one does not;
quality checks (specialty, city, facility) are reported, and fail the run only with
`E2E_STRICT_QUALITY=1`.

Optional, before `up`:

- NER: with a verified artifact in the gateway's `compose/models/pii` and `CALIBAN_PII_NER_DIR` set
  in `compose/.env`, run with `E2E_EXPECT_NER=1` so names must be masked.
- Negative control for the injection row: `E2E_FOLLOW_INJECTIONS=1` only affects the scripted
  model, not a real one.

Keep the report: copy `e2e/health-triage/.work/report-*.md` and `results-*.json` to the testbed
bucket's `results/` prefix (with `results_upload = true`) or paste them from the session.

## 4. Clean up

```sh
e2e/health-triage/e2e.sh down      # e2e containers, volumes and network; caliban back on its own config
```

Then destroy the testbed (`tofu destroy`, same variables). `down` leaves the tenant `medmarket`
and its node in the gateway's Postgres; they go with the testbed.
