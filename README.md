<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/thecalibanproject/website/main/public/brand/logo-white.svg">
  <img alt="Caliban" src="https://raw.githubusercontent.com/thecalibanproject/website/main/public/brand/logo.svg" width="200">
</picture>

# Caliban Deploy

Container image, single-host compose stack, Helm chart and air-gapped bundles for running Caliban on-prem with zero egress.

[Docs](https://github.com/thecalibanproject/docs) · [Core](https://github.com/thecalibanproject/core) · [Web console](https://github.com/thecalibanproject/web) · [ML](https://github.com/thecalibanproject/ml)

## What this is

Caliban is a sovereign AI gateway: one OpenAI- and Anthropic-compatible endpoint for a company, with PII screening and pseudonymisation, intent routing, caching, metering, and on-prem open-weight models. The server is a single Rust binary built from [core](https://github.com/thecalibanproject/core); the admin console comes from [web](https://github.com/thecalibanproject/web).

This repo packages both into one image and runs it in three shapes: a single host with Docker Compose, Kubernetes with Helm, and fully air-gapped sites fed from signed offline bundles. It also serves open-weight models (vLLM, SGLang, TEI, llama.cpp, Ollama) next to Caliban, sized per GPU tier.

Two rules hold in every shape:

- **Zero egress by default.** The control plane, web console, datastores and models all run locally. Nothing calls home, and telemetry is off unless you point it at your own collector.
- **BYOK.** Tenants bring their own provider keys, or use local models. Caliban never pools upstream credentials. Opening egress to a provider is an explicit, per-provider decision.

**Status:** in active development with design partners. No Caliban image is published to a public registry: build it from source (below) or load it from a bundle.

## Contents

```
deploy/
├── images/
│   ├── caliban.Dockerfile         multi-stage: web (node:24-slim) + core (rust:1-trixie)
│   │                              -> gcr.io/distroless/cc-debian13:nonroot
│   ├── caliban.Dockerfile.dockerignore
│   └── build.sh                   build/push wrapper; `build.sh vendor` for hermetic builds
├── compose/
│   ├── docker-compose.yml         caliban + postgres + qdrant + valkey, one profile per model pool
│   ├── docker-compose.byok-egress.yml   opt-in egress for BYOK providers
│   ├── .env.example
│   └── config/caliban.toml        on-prem config matching the compose model pools
├── helm/caliban/                  chart: router / control plane, model pools, NetworkPolicy, PSS restricted
│   ├── values.yaml
│   ├── values-airgap.yaml
│   └── README.md
├── airgap/
│   ├── bundle.sh, load.sh         fetch pinned weights; create, verify and load signed offline bundles
│   ├── images.lock                pinned images
│   ├── models.lock.yaml           pinned weights per tier (Apache-2.0 / MIT by default)
│   └── README.md                  full offline procedure
├── aws/testbed/                   OpenTofu: short-lived AWS test environment (benchmarks, GPU tier,
│                                  zero-egress install); see its README
└── scripts/smoke.sh               health + chat completion check against a running deployment
```

## Deployment shapes

Every shape runs the same image and the same binary. Background: §6 of the [reference architecture](https://github.com/thecalibanproject/docs/blob/main/architecture/caliban-reference-architecture.md).

| Shape | Use it for | Caliban | Datastores | Models | Egress |
|---|---|---|---|---|---|
| **Single-host compose** (`compose/`) | POCs, small sites, edge, demo appliances | `caliban standalone` (router and control plane in one process) | Postgres 17, Qdrant and Valkey containers | one container per model (vLLM, TEI, llama.cpp or Ollama), one compose profile each | none; BYOK through an opt-in override |
| **Kubernetes** (`helm/caliban`) | VPC or dedicated clusters, production | `mode: split` (router Deployment with optional HPA, fed signed config snapshots by the control plane) or `mode: standalone` | bring your own (CloudNativePG, Qdrant chart, Valkey chart) | chart `modelPools` (vLLM, SGLang, TEI, llama.cpp), or your own llm-d or Dynamo | default-deny NetworkPolicy; allow-list per provider |
| **Air-gapped** (`airgap/`) | regulated and classified sites | either of the above, fed from a signed bundle | same | open-weight only, pinned and hashed | none, by construction |

**Ports.** `8080` is the data plane (`/v1/*` OpenAI- and Anthropic-compatible API, `/healthz`). `8081` is the control plane (`/api/v1/*` and the web console at `/`).

**Datastores.** Postgres is the control plane's store: it holds tenants, keys, sealed BYOK credentials, the catalogue, routes and the hash-chained audit log, and core applies its migrations at start-up. Valkey holds the rate limits and token budgets (`CALIBAN_VALKEY_URL`, `[limits] store = "valkey"` in the shipped configs), so every router enforces the same quotas; if it is unreachable, each router limits locally and `/healthz` reports `quota.state: degraded`. Qdrant is provisioned and wired in (`CALIBAN_QDRANT_URL`) for the semantic cache and retrieval index; those features are still in progress in core, and the current binary does not connect to Qdrant yet.

## Build the image

The Dockerfile's build context is a directory that holds [core](https://github.com/thecalibanproject/core) and [web](https://github.com/thecalibanproject/web) as siblings of this repo:

```bash
mkdir caliban && cd caliban
git clone https://github.com/thecalibanproject/core
git clone https://github.com/thecalibanproject/web
git clone https://github.com/thecalibanproject/deploy

deploy/images/build.sh --version 0.1.0          # -> caliban/caliban:0.1.0, loaded into local Docker
```

`build.sh` options: `--version`, `--image` (default `caliban/caliban`), `--registry`, `--platform`, `--offline`, `--push`, `--sbom` (SBOM and provenance attestations, with `--push`) and `--build-arg`. Set `CALIBAN_SRC` if the sources live elsewhere. Without `--version` the tag comes from `git describe` in `core`.

What the image contains:

- `/usr/local/bin/caliban`, built with the `ner` feature by default (`--build-arg CARGO_FEATURES=` to drop it). The `ner` feature links a prebuilt ONNX Runtime built with GCC 14, so the builder is `rust:1-trixie` and the runtime is `gcr.io/distroless/cc-debian13:nonroot`.
- The console at `/usr/share/caliban/web`.
- `CALIBAN_CONFIG=/etc/caliban/caliban.toml`, `CALIBAN_WEB_DIR=/usr/share/caliban/web`, `CALIBAN_LOG=info`. It runs as uid 65532, exposes 8080 and 8081, and defaults to `standalone`. There is no shell: use `caliban healthcheck` for container healthchecks.

Base images are build args (`NODE_IMAGE`, `RUST_IMAGE`, `RUNTIME_IMAGE`) so they can point at a private mirror; pin them by digest for reproducible builds.

**Hermetic builds.** On a connected machine, `deploy/images/build.sh vendor` writes `core/vendor/`, `core/.cargo-vendor-config.toml`, `web/.pnpm-store/` and `web/.corepack/corepack.tgz`. Carry the source tree and the three base images to the offline builder and run `deploy/images/build.sh --offline --version 0.1.0` (which builds with `--network=none`). With `ner`, provide ONNX Runtime through `ORT_LIB_LOCATION` (see core's [`MODELS.md`](https://github.com/thecalibanproject/core/blob/main/crates/caliban-pii/MODELS.md)) or build without it.

## Quick start: single host (compose)

```bash
# 1. Image: build it (above) or load it from a bundle (airgap/README.md)
deploy/images/build.sh --version 0.1.0

# 2. Secrets
cd deploy/compose
cp .env.example .env && chmod 600 .env
#    fill in CALIBAN_ADMIN_TOKEN, CALIBAN_KEK, POSTGRES_PASSWORD, VALKEY_PASSWORD
#    (.env.example shows the openssl commands)

# 3. Weights into ./models (connected machine; licence-gated, pinned revisions)
../airgap/bundle.sh fetch --profile qwen3-small,embeddings,reranker --models-dir ./models
#    fetch refuses unpinned revisions: pin models.lock.yaml first, or pass --allow-unpinned

# 4. Start the pools for your hardware tier (see "Running open models")
docker compose --profile qwen3-small --profile embeddings --profile reranker up -d   # 1 × 24 GB GPU
docker compose --profile cpu up -d                                                  # CPU only

# 5. Tenant API key, then smoke test
MINT_KEY=1 ../scripts/smoke.sh     # mints a key through the admin API and runs a chat completion
```

To mint a key offline instead, run `docker compose run --rm caliban keygen` and put the hash in `config/caliban.toml` under `tenants.api_key_hashes` before the first start (see the note on seeding below).

**Health.** The `caliban` service has a Docker healthcheck that runs the binary itself (`caliban healthcheck --addr 127.0.0.1:8080 --path /healthz`), since the distroless image has no shell or curl. `docker compose ps` shows it as `healthy` once it serves, `docker compose up -d --wait` returns only then, and other services can wait for it with `depends_on: { caliban: { condition: service_healthy } }`.

The web console is at `http://127.0.0.1:8081/`; log in with `CALIBAN_ADMIN_TOKEN`. The ports bind to `127.0.0.1` by default (`CALIBAN_BIND`, `CALIBAN_ROUTER_PORT`, `CALIBAN_CP_PORT`). Put a TLS reverse proxy in front before exposing them.

**Config seeding.** The compose stack runs with Postgres (`CALIBAN_DATABASE_URL`), so `config/caliban.toml` seeds the database **once**, on the first start. After that, tenants, API keys, BYOK credentials, providers, models and routes are managed in the console or through the admin API, and those sections of the file are ignored. `[server]`, `[security]`, `[cache]`, `[pii]` and `[limits]` are always read from the file.

**Network layout.**

- `postgres`, `qdrant`, `valkey` and every model server sit only on `backend`, which is `internal: true` and has no route off the host.
- `caliban` also joins `edge`, a bridge with IP masquerading disabled. That is enough to publish 8080 and 8081, but containers on it cannot reach the internet.
- For a hard guarantee, add host firewall rules as well (see the hardening checklist).

**Smoke test options** (`scripts/smoke.sh`): `CALIBAN_URL`, `CALIBAN_ADMIN_URL`, `CALIBAN_ADMIN_TOKEN` (read from `compose/.env` if unset), `CALIBAN_API_KEY`, `MINT_KEY=1`, `TENANT` (default `default`), `MODEL` (default `caliban/auto`), `TIMEOUT` and `SKIP_CHAT=1`.

## Running open models

Every model runs in its own engine container (compose) or pool (Helm). Caliban sees each one as a deployment-wide `[[providers]]` entry; its `[[models]]` entries describe what the model can do (tools, vision, how reasoning is switched, context). Tenant routes decide which intent goes where. The full catalogue, with every repo ID and licence checked, is core's [`config/open-models.example.toml`](https://github.com/thecalibanproject/core/blob/main/config/open-models.example.toml); `compose/config/caliban.toml` is generated from it. Parser flags, VRAM maths and background are in [research note 09](https://github.com/thecalibanproject/docs/blob/main/research/09-open-models-on-prem.md).

Compose profiles: `qwen3-small`, `qwen3-large`, `qwen3-moe`, `gpt-oss`, `embeddings`, `reranker`, `cpu`, `ollama`. With no profile, only Caliban and the datastores start (BYOK only, which needs the egress override).

### Hardware tiers (estimates; benchmark per site)

| Tier | Chat | Embeddings | Reranker | Compose profiles |
|---|---|---|---|---|
| **CPU only** (32 GB+ RAM, 16+ cores) | Qwen3.6-35B-A3B Q4_K_M GGUF on llama.cpp (20.4 GB; 3B active) | Qwen3-Embedding-0.6B on TEI (CPU) | bge-reranker-v2-m3 on TEI (CPU) | `cpu` |
| **1 × 24 GB** (L4, A10, RTX 4090) | Qwen3.5-9B on vLLM, online FP8 (about 11 GB of weights, 128k context); or gpt-oss-20b | Qwen3-Embedding-0.6B (TEI) | Qwen3-Reranker-0.6B (vLLM pooling) | `qwen3-small embeddings reranker` (or `gpt-oss …`) |
| **1 × 48 GB** (L40S, RTX 6000 Ada) | Qwen3.8-27B-FP8 (30.9 GB; 128k context with FP8 KV); throughput option: Qwen3.6-35B-A3B-FP8 (`qwen3-moe`) | Qwen3-Embedding-0.6B | Qwen3-Reranker-0.6B | `qwen3-large embeddings reranker` |
| **1 × 80 GB** (H100, A100-80G) | Qwen3.8-27B-FP8 at 262k (`QWEN3_LARGE_MAX_MODEL_LEN=262144`); or gpt-oss-120b | Qwen3-Embedding-4B (Helm) or 0.6B | Qwen3-Reranker-4B (Helm) or 0.6B | `qwen3-large embeddings reranker` |
| **2 to 8 × 80 GB** | Qwen3.5-122B-A10B-FP8 (TP2), Qwen3.5-397B-A17B-FP8 (TP8) or DeepSeek-V4-Flash (MIT), plus a 1-GPU Qwen3.8-27B fast tier | Qwen3-Embedding-8B | Qwen3-Reranker-8B | Helm `modelPools` (e.g. `qwen3-xl`) |

- **One embedder per deployment.** Its dimension (1024, 2560 or 4096) is baked into the vector collections, so changing the embedder means re-indexing.
- **Ollama** (`--profile ollama`) is a single endpoint serving many models (`qwen3.6:35b`, `qwen3-embedding:0.6b`, …). It suits small sites. It has no rerank endpoint, and its library tags cannot be pinned by revision.
- **Sharing a GPU.** Several pools can share one GPU (`*_GPU` picks the device) as long as the vLLM `*_GPU_UTIL` fractions plus about 1.5 GB for TEI fit on the card. The `.env.example` defaults fit `qwen3-small + embeddings + reranker` on 24 GB, and `qwen3-large + embeddings + reranker` on 48 GB. For tensor parallelism, list several ids in `device_ids` and raise the `*_TP` value.
- **Profile conflicts.** Do not combine `embeddings` and `cpu`: both serve the `embed` name.
- **Offline engines.** Every engine runs with `HF_HUB_OFFLINE=1` and the vLLM/HF telemetry opt-outs, and reads weights read-only from `$MODELS_DIR`. gpt-oss gets `TIKTOKEN_ENCODINGS_BASE` so its tokenizer does not try to download encodings at start-up.

### Adding a model

1. **Check the licence.** Look at `cardData.license` on Hugging Face. Apache-2.0 and MIT pass the bundle gate; anything else needs legal review and `--allow-licence`.
2. **Add it to `airgap/models.lock.yaml`** with the repo ID, the pinned `revision`, the licence and a `dir`. Fetch it on a connected machine with `airgap/bundle.sh fetch --model <repo-id> --pin`, which downloads with `hf download --revision`, records the tree sha256 and writes it into the lock.
3. **Serve it.** Copy one of the vLLM services in `compose/docker-compose.yml`, or a Helm `modelPools.pools` entry.
   - Set `--served-model-name` to the repo ID.
   - Add the right `--tool-call-parser` and `--reasoning-parser` for the family (table in research note 09, §3).
   - Embedders run on TEI or on vLLM with `--runner pooling`. Rerankers run on TEI (XLM-R, GTE) or vLLM pooling.
4. **Declare it to Caliban.** You need a provider (the pool's `base_url`; set `cache_salt = true` only for vLLM and SGLang) and a model entry with `upstream_model = "<served name>"`, `kind`, `family`, `licence`, `context_window` (the served length) and capabilities: `tools`, `vision`, `reasoning` (`none|always|hybrid`), `reasoning_control` (`none|enable_thinking|reasoning_effort`) and `inline_think_tags`. Then reference the model in tenant routes.
   - After the first start (Postgres is the source of truth), do this in the console's model pages, which can discover what a server serves and suggest catalogue entries, or through the admin API: `POST /api/v1/providers`, `POST /api/v1/providers/{id}/discover`, `POST /api/v1/models` and `PUT /api/v1/tenants/{id}/routes`.
   - Before the first start, or with Helm `config` in file-driven mode, add `[[providers]]` and `[[models]]` entries to the config.
5. **Test.** Run `MODEL=<model id> MINT_KEY=1 scripts/smoke.sh`.

### PII NER model

Caliban's L1 PII detector runs in-process, not as a model server. Fetch the artifact on a connected machine with the ml repo's [`scripts/fetch_pii_ner.py`](https://github.com/thecalibanproject/ml/blob/main/scripts/fetch_pii_ner.py), which writes the hash-verified `manifest.json` Caliban requires, and copy `ml/artifacts/pii_ner/` to `CALIBAN_PII_MODEL_DIR` (compose default `./models/pii`, mounted read-only at `/models/pii`). `CALIBAN_PII_NER_DIR` selects the artifact inside the container (default `/models/pii/nym-pii-multilingual-small-int8/3.0.0`); leave it empty to turn L1 off. If it is set and verification fails, Caliban refuses to start. A licence sign-off on the model's fine-tuning data is still pending; see core's [`MODELS.md`](https://github.com/thecalibanproject/core/blob/main/crates/caliban-pii/MODELS.md).

## Kubernetes (Helm)

The chart takes secrets through existing Secrets. Push the image to a registry the cluster can pull from first (for example `images/build.sh --registry registry.internal:5000 --version 0.1.0 --push`) and set `global.imageRegistry` or `image.registry`.

```bash
kubectl create ns caliban && kubectl label ns caliban pod-security.kubernetes.io/enforce=restricted
kubectl -n caliban create secret generic caliban-auth \
  --from-literal=admin-token="$(openssl rand -hex 32)" --from-literal=kek="$(openssl rand -base64 32)"
kubectl -n caliban create secret generic caliban-database --from-literal=url='postgres://...'
# split mode only: snapshot signing key pair and router token
eval "$(docker run --rm caliban/caliban:0.1.0 gen-signing-key | sed 's/ *#.*//')"
kubectl -n caliban create secret generic caliban-snapshot \
  --from-literal=signing-key="$CALIBAN_SNAPSHOT_SIGNING_KEY" \
  --from-literal=public-key="$CALIBAN_SNAPSHOT_PUBLIC_KEY" \
  --from-literal=router-token="$(openssl rand -hex 32)"
helm install caliban helm/caliban -n caliban                                        # connected / VPC
helm install caliban helm/caliban -n caliban -f helm/caliban/values-airgap.yaml     # air-gapped
```

Key values (full table in [`helm/caliban/README.md`](helm/caliban/README.md)): `mode` (`split` or `standalone`; `values-airgap.yaml` uses `standalone`), `router.mode` (`snapshot` or `static`), `global.imageRegistry`, `image.digest`, `auth.existingSecret`, `database.existingSecret`, `snapshotKeys.existingSecret`, `providerKeys.existingSecret`, `qdrant.url`, `valkey.url` / `valkey.existingSecret`, `otel.endpoint`, `config` (rendered verbatim to `caliban.toml`), `router.autoscaling.enabled`, `modelPools`, `networkPolicy.*`.

Every pod runs as non-root (uid 65532) with a read-only root filesystem, all capabilities dropped, `seccompProfile: RuntimeDefault`, no service-account token and `enableServiceLinks: false`, which meets the `restricted` Pod Security Standard. The NetworkPolicy is default-deny: egress goes only to cluster DNS, the other Caliban pods, the datastores, local model servers and any providers you list.

**Split mode in the chart.** With `mode: split`, `router.mode` picks where the routers get tenants, keys, BYOK credentials, routes and the rest of the config. See core's [split mode](https://github.com/thecalibanproject/core#split-mode) for the protocol.

- **`snapshot` (default).** Routers run with no config file. They poll `GET /api/v1/snapshot` on the control plane every `router.snapshot.pollIntervalSeconds` (default 10, with ±20% jitter), verify the Ed25519 signature and swap the new config in. Anything created in the console or admin API reaches every router within about one poll interval. The chart wires it up as follows:
  - The `caliban-snapshot` Secret (`snapshotKeys.existingSecret`) holds `signing-key`, `public-key` and `router-token`. The control plane gets `CALIBAN_SNAPSHOT_SIGNING_KEY` and `CALIBAN_ROUTER_TOKEN`; routers get `CALIBAN_SNAPSHOT_PUBLIC_KEY` and `CALIBAN_ROUTER_TOKEN`, so the signing key never reaches a router container. Set `snapshotKeys.routerExistingSecret` to a Secret holding only the public key and router token to keep the signing key off router-only nodes as well.
  - Routers reach the control plane at `http://<release>-control-plane:8081` (override with `router.snapshot.controlPlaneUrl`). The NetworkPolicy already allows router-to-control-plane traffic.
  - Routers keep `CALIBAN_KEK`: sealed BYOK credentials stay sealed inside the snapshot and are opened on the router, and the KEK keeps the per-tenant `cache_salt` identical across routers. Keys referenced as `{ env = "..." }` resolve on the router too, so `providerKeys.existingSecret` is still mounted there.
  - **Fail-static.** If the control plane is down, or a snapshot fails verification or validation, or is older than the one being served, the router logs it and keeps serving its last good snapshot. The last good snapshot is also written to an `emptyDir` (`router.snapshot.cache.enabled`, `CALIBAN_SNAPSHOT_CACHE`) and re-verified on load, so a router container that restarts while the control plane is down still serves. A brand-new router pod has no cache: it waits for its first snapshot before it listens, so it stays unready until the control plane answers.
- **`static`.** Routers read the rendered `config` from the ConfigMap. Console and admin API changes reach the control plane only, never the routers, so keep tenants, keys and routes in `config`. No snapshot Secret is needed.

In both modes router pods get no `CALIBAN_ADMIN_TOKEN` and no `CALIBAN_DATABASE_URL`: core reads them only when it builds the control plane. Global `extraEnv` still applies to every Caliban pod; use `router.extraEnv`, `controlPlane.extraEnv` or `standalone.extraEnv` for anything that must stay on one component.

**Model pools.** `modelPools.pools` renders one Deployment and Service per enabled pool (vLLM, SGLang, TEI or llama.cpp; examples in `values.yaml` are disabled). Weights come read-only from a PVC with the bundle's `models/` layout; pools get no egress, accept ingress only from Caliban, request `nvidia.com/gpu` via `gpus: N`, and roll out with `Recreate`. You can also run vLLM, SGLang, llm-d or NVIDIA Dynamo yourself and list them in `networkPolicy.egress.localModels`.

## Air-gapped

See [airgap/README.md](airgap/README.md) for the full procedure. In short:

1. **Connected side.** `bundle.sh fetch` downloads the pinned weights, licence-gated, and records tree hashes. `bundle.sh --version <v> --profile <profiles> --sign minisign|cosign --key <file>` saves the pinned images, copies the weights and this repo, writes `MANIFEST.sha256` and signs it. The output is `caliban-bundle-<v>.tar` plus `.tar.sha256`.
2. **Site.** `load.sh caliban-bundle-<v>.tar --pubkey <file>` checks the signature against a public key received out of band, verifies every file, refuses unlisted files and unsafe paths, then `docker load`s the images or, with `--registry`, pushes them to the site registry. `--models-dest` copies the weights and `--env-out` writes compose image overrides.

`--profile` takes the compose profile names plus `k8s` (Helm-only weights), `all` (default) and `none` (Caliban and datastores only). The default bundle contains only Apache-2.0 and MIT weights; `bundle.sh` refuses other licences unless you pass `--allow-licence <id>` after legal review.

## AWS testbed

[`aws/testbed/`](aws/testbed/README.md) is OpenTofu code for a short-lived test environment in eu-central-1 that runs this repo's compose stack, images and bundles on EC2. It covers four runbooks: the gateway overhead benchmark, split mode with N routers and a shared Valkey, the 1 × 48 GB GPU tier (Qwen3.8-27B-FP8, Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B on an L40S), and a zero-egress install from an offline bundle in a subnet with no internet route.

- Every host is off unless enabled. Access is SSM Session Manager only, with no SSH and no inbound port from outside the VPC.
- Hosts stop themselves after `ttl_hours` (default 4), and `tofu destroy` removes everything.
- Read its README for the prerequisites, the cost table and the teardown checklist before the first apply.

## Sizing rules of thumb

These are **estimates; benchmark per site.** The model-serving numbers come from [research note 07](https://github.com/thecalibanproject/docs/blob/main/research/07-gateway-serving-and-sovereign-deployment.md) ("GPU sizing rules of thumb" and "On-prem model serving recommendation").

**GPU memory for a model**

- **Weights ≈ params × bytes per param.** FP16/BF16 = 2, FP8 = 1, INT4 ≈ 0.5 to 0.6.
- **KV cache per token ≈ 2 × layers × kv_heads × head_dim × bytes.** Example: 32 layers, 8 KV heads and head_dim 128 at FP16 need 2·32·8·128·2 = 128 KiB per token, so 32 concurrent 8k-token sequences need about 32 GiB of KV.
- **Reserve 20 to 40% of HBM for KV** at your target concurrency. In compose, vLLM's `--gpu-memory-utilization` (per pool: `QWEN3_SMALL_GPU_UTIL`, `RERANK_GPU_UTIL`, …) sets the budget. Qwen3.5, 3.6 and 3.8 keep KV for only 1 layer in 4 (the rest are DeltaNet layers), so their per-token KV is 20 to 64 KiB rather than the 128 KiB of the dense example.

**Reference points**

- gpt-oss-20b fits a **16 GB** GPU; gpt-oss-120b fits **1 × 80 GB**.
- Frontier open MoE models (DeepSeek-V4-Pro, about 1.7T params) need multiple nodes with prefill/decode disaggregation (llm-d or NVIDIA Dynamo).

**Quantization:** FP8 on Hopper and Blackwell, AWQ INT4 on memory-bound sites, SmoothQuant W8A8 on Ampere.

**Engines:** vLLM by default; SGLang for agent and RAG pools with heavy prefix reuse; llama.cpp for CPU, edge and development. Do not use TGI: it has been in maintenance mode since December 2025 and was archived in March 2026.

**CPU profile** (llama.cpp, Qwen3.6-35B-A3B Q4_K_M GGUF, about 20.4 GB, 3B active parameters): plan for 32 GB of RAM or more (64 GB is comfortable) and 16+ physical cores. Expect single-digit to low double-digit tokens/s per stream. Use it for demos and low-volume internal use, not for production SLOs. Smaller options: Qwen3.5-9B Q4_K_M (5.7 GB) or gpt-oss-20b MXFP4 (12.1 GB).

**Caliban itself.** The design target for gateway overhead is p50 < 3 ms and p99 < 10 ms, excluding classifiers and RAG. The figures below are starting points only; measure with your own traffic before committing to capacity.

| Component | Starting point |
|---|---|
| router | 0.5 vCPU / 512 MiB per replica, 2+ replicas, scale on CPU. SSE streams are long-lived, so scale down slowly. |
| control plane | 0.25 vCPU / 256 MiB |
| Postgres | 2 vCPU / 4 GiB; SSD; nightly `pg_dump` plus WAL archiving |
| Qdrant | RAM ≈ vectors × dim × 4 B × 1.5 (or on-disk / quantized collections); snapshots enabled |
| Valkey | 1 GiB, AOF on |

## Security hardening checklist

**Network**

- [ ] Datastores and model servers have no route out: the compose `internal: true` backend, or the Kubernetes default-deny NetworkPolicy.
- [ ] A host firewall backs up the container network config. In the `DOCKER-USER` chain, drop forwarded traffic from the Caliban bridges except to explicitly allowed provider ranges.
- [ ] Published ports bind to `127.0.0.1` or an internal NIC, with TLS terminated at your proxy or ingress. Use mTLS for internal clients where possible.
- [ ] The control plane (8081) is **not** exposed to the same audience as the data plane: separate ingress, or VPN only.
- [ ] No outbound DNS from Caliban hosts. Verify with firewall and DNS logs during a smoke test.

**Secrets and keys**

- [ ] `CALIBAN_KEK` is 32 random bytes, stored in a vault, HSM or sealed secret, **backed up offline**, and never in git or Helm values (`auth.create=false`). Losing it makes every stored BYOK key unrecoverable.
- [ ] `CALIBAN_ADMIN_TOKEN` is 32+ random bytes and rotated on staff changes.
- [ ] Postgres and Valkey have unique strong passwords. Postgres uses `scram-sha-256` and TLS (`sslmode=require`) when it runs off-host.
- [ ] `.env` is `chmod 600` and owned by the service account.

**Workloads**

- [ ] Caliban runs as non-root (65532) with a read-only root filesystem, `cap_drop: ALL` and `no-new-privileges`. The namespace enforces the `restricted` Pod Security Standard.
- [ ] Images are pinned by digest and loaded from a signed bundle or a private mirror. Verify image signatures and SBOMs (`build.sh --push --sbom`) in admission where supported.
- [ ] Model weights are loaded read-only from pinned, hashed directories, with `HF_HUB_OFFLINE=1`. vLLM runtime LoRA loading stays **off** on shared pools.
- [ ] Only Apache-2.0 or MIT weights unless legal approved otherwise. Llama 4 is blocked for EU tenants. Qwen3.8-Flash-Next and Qwen3.8-2.4T (custom Qwen licences), Mistral Medium 3.5 and Devstral 2 123B (revenue cap) and MiniMax M2.7 (non-commercial) are not permissive.
- [ ] Shared vLLM and SGLang pools have `cache_salt = true` on their provider entry (tenant prefix-cache isolation). llama.cpp, Ollama and TEI pools serve one trust group only.

**Data**

- [ ] `security.egress = "deny_by_default"` stays in `caliban.toml`.
- [ ] PII mode `reversible` or `mask` is set for tenants handling personal data. ZDR is set where required.
- [ ] `OTEL_EXPORTER_OTLP_ENDPOINT` is empty or points at your own collector only. Caliban never records prompt or response content in spans.
- [ ] Backups (Postgres, Qdrant snapshots, KEK) are tested by restoring them on a scratch host.

## BYOK and egress

There are two ways to give Caliban a tenant's provider key.

1. **Admin console or API (recommended).** The key is sealed with AES-256-GCM under `CALIBAN_KEK` before it is stored, and it is never returned.

   ```bash
   curl -X POST http://127.0.0.1:8081/api/v1/tenants/acme/provider-keys \
     -H "Authorization: Bearer $CALIBAN_ADMIN_TOKEN" -H 'Content-Type: application/json' \
     -d '{"kind":"openai","label":"OpenAI prod","provider_id":"openai",
          "base_url":"https://api.openai.com/v1","api_key":"sk-...","trust_tier":"t2_contracted"}'
   ```

2. **Config reference.** In `caliban.toml`, declare `api_key = { env = "ACME_OPENAI_API_KEY" }` under `[[tenants.providers]]`, and supply the variable through `.env` (compose) or `providerKeys.existingSecret` (Helm).

Either way, the provider's model must be in the catalogue and in the tenant's routes. `security.egress = "deny_by_default"` makes Caliban refuse any `base_url` that is not declared.

**Then open the network path for that provider only:**

- **Compose.** Add the override; only the `caliban` container gets a route out:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.byok-egress.yml --profile qwen3-small up -d
  ```

  Restrict the `br-caliban-egr` bridge in `DOCKER-USER` to the provider's ranges, or send traffic through your egress proxy (`CALIBAN_HTTPS_PROXY`, `CALIBAN_NO_PROXY`). Example rules are in the override file.
- **Kubernetes.** List the provider under `networkPolicy.egress.providers` with its `cidrs`. With Cilium, set `networkPolicy.cilium.enabled=true` and list `hosts` (e.g. `api.openai.com`) for an FQDN allow-list.
- **Air-gapped.** No BYOK providers. Remove, or keep commented out, every non-local provider.

Trust tiers (`t0_sovereign` to `t3_public`) on providers and models let tenant policy keep sensitive traffic on local models even when a BYOK provider is configured.

## Upgrade and rollback

The control plane applies its Postgres migrations at start-up (they are embedded in the binary and checksummed in `caliban_schema_migrations`). **Treat migrations as forward-only.** Before any upgrade:

- back up Postgres (`pg_dump -Fc`);
- take a Qdrant snapshot;
- confirm you have the KEK.

Rolling back across a migration means restoring that backup.

**Compose**

```bash
docker compose exec postgres pg_dump -U caliban -Fc caliban > backup-$(date +%F).dump
docker compose stop qdrant && docker run --rm --entrypoint tar -v caliban_qdrant-data:/d:ro \
  -v "$PWD":/b postgres:17.11-bookworm -C /d -cf /b/qdrant-$(date +%F).tar . && docker compose start qdrant
# upgrade: build (images/build.sh --version 0.2.0) or load (airgap/load.sh) the new image first
sed -i 's/^CALIBAN_VERSION=.*/CALIBAN_VERSION=0.2.0/' .env
docker compose up -d caliban
../scripts/smoke.sh
# rollback
sed -i 's/^CALIBAN_VERSION=.*/CALIBAN_VERSION=0.1.0/' .env
docker compose up -d caliban    # if 0.2.0 migrated the schema, restore the dump first
```

**Helm**

```bash
helm upgrade caliban helm/caliban -n caliban -f my-values.yaml --set image.digest=sha256:<new>
kubectl -n caliban rollout status deploy/caliban-router
helm rollback caliban <REVISION> -n caliban        # restores the previous manifests and config
```

- Caliban Deployments roll out with `maxUnavailable: 0`, and in split mode the router PDB keeps at least one router serving.
- Config changes trigger a rollout through the `checksum/config` annotation.
- Upgrading a split-mode release from a chart version without `router.mode`: routers now default to `snapshot`, so create the `caliban-snapshot` Secret (see "Kubernetes (Helm)") before `helm upgrade`, or set `router.mode=static` to keep the old behaviour. Without the Secret, router pods fail with `CreateContainerConfigError` while the old ones keep serving (`maxUnavailable: 0`).
- In split mode with `router.mode: snapshot`, routers keep serving their last good snapshot while the control plane restarts (fail-static). Core's rule is to upgrade routers before the control plane when a release adds config fields. A single `helm upgrade` rolls both together, and an old router that cannot read a newer snapshot keeps serving its last good one until it is replaced.

**Air-gapped:** the same steps with a new bundle. Keep the previous bundle as rollback media until the new version is accepted.

**Pins to bump together:**

- `airgap/images.lock`
- the `*_IMAGE` defaults in `compose/docker-compose.yml` and `.env.example`
- `Chart.yaml` `appVersion`
- `models.lock.yaml` revisions and hashes

## Validation

```bash
for p in qwen3-small qwen3-large qwen3-moe gpt-oss embeddings reranker cpu ollama; do
  docker compose -f compose/docker-compose.yml --env-file compose/.env --profile "$p" config -q || echo "FAIL $p"
done
docker run --rm -v "$PWD":/apps alpine/helm:3 lint --strict /apps/helm/caliban
docker run --rm -v "$PWD":/apps alpine/helm:3 lint --strict /apps/helm/caliban -f /apps/helm/caliban/values-airgap.yaml
docker run --rm -v "$PWD":/apps alpine/helm:3 template caliban /apps/helm/caliban -f /apps/helm/caliban/values-airgap.yaml
docker run --rm -v "$PWD":/apps alpine/helm:3 template caliban /apps/helm/caliban                          # split, router.mode=snapshot
docker run --rm -v "$PWD":/apps alpine/helm:3 template caliban /apps/helm/caliban --set router.mode=static
docker run --rm -v "$PWD":/mnt -w /mnt koalaman/shellcheck:stable -x images/build.sh airgap/*.sh scripts/smoke.sh
docker buildx build --check -f images/caliban.Dockerfile ..
```

Run these from the repo root. The last command uses the parent directory (holding `core` and `web`) as build context.

## Related repositories

| Repo | What it holds |
|---|---|
| [core](https://github.com/thecalibanproject/core) | The `caliban` binary (Rust workspace), config format, OpenAPI spec, migrations |
| [web](https://github.com/thecalibanproject/web) | Admin console, bundled into the image |
| [docs](https://github.com/thecalibanproject/docs) | Reference architecture and research notes (serving, open models, sovereign deployment) |
| [ml](https://github.com/thecalibanproject/ml) | In-process model artifacts, including the PII NER fetch script |
| [sdk-typescript](https://github.com/thecalibanproject/sdk-typescript), [sdk-python](https://github.com/thecalibanproject/sdk-python) | Client SDKs (Apache-2.0) |

## Licence

Copyright 2026 Elie Sfeir. All rights reserved.

This repository is proprietary and source-available. It is public for reference and evaluation only and is not open source. No right to use, copy, modify or distribute it is granted except under a written agreement with the copyright holder. See [LICENSE](LICENSE). For licensing, contact [elie@internalizable.dev](mailto:elie@internalizable.dev).

Third-party images and model weights referenced here remain under their own licences.
