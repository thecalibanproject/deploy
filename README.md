# Caliban — deploy

This repo has everything needed to run Caliban **on-prem with zero egress**: the container
image build, a single-host docker-compose stack, a Helm chart, and air-gap bundle tooling.

Two rules hold in every deployment shape:

- **Zero egress by default.** The control plane, web console, datastores and models all run
  locally. Nothing calls home, and telemetry is off unless you point it at your own collector.
- **BYOK.** Customers bring their own provider keys, or use local models. Caliban never pools
  upstream credentials. Opening egress to a provider is an explicit, per-provider decision.

```
deploy/
├── README.md                      (this file)
├── images/
│   ├── caliban.Dockerfile         multi-stage: web (node) + core (rust) -> distroless nonroot
│   ├── caliban.Dockerfile.dockerignore
│   └── build.sh                   build/push wrapper; `build.sh vendor` for hermetic builds
├── compose/
│   ├── docker-compose.yml         caliban + postgres + qdrant + valkey (+ one profile per model pool)
│   ├── docker-compose.byok-egress.yml   opt-in egress for BYOK providers
│   ├── .env.example
│   └── config/caliban.toml
├── helm/caliban/                  chart (router/control-plane, model pools, NetworkPolicy, PSS-restricted)
│   ├── values.yaml
│   └── values-airgap.yaml
├── airgap/
│   ├── bundle.sh  load.sh         fetch pinned weights; signed offline bundle: create / verify + load
│   ├── images.lock                pinned images
│   ├── models.lock.yaml           pinned weights per tier (Apache-2.0 / MIT only)
│   └── README.md                  full offline procedure
└── scripts/smoke.sh               health + chat completion check
```

## Deployment SKUs

Every SKU runs the same image and the same binary. See §6 of
`docs/architecture/caliban-reference-architecture.md`.

| SKU | Use it for | Caliban | Datastores | Models | Egress |
|---|---|---|---|---|---|
| **Single-host compose** (`compose/`) | POCs, small sites, edge, demo appliances | `caliban standalone` (router + control plane in one process) | Postgres 17, Qdrant, Valkey containers | one container per model: vLLM, TEI, llama.cpp or Ollama, one compose profile each (see "Running open models") | none; BYOK via an opt-in override |
| **Kubernetes** (`helm/caliban`) | VPC / dedicated, production | `mode: split`: stateless router (HPA) + control plane. `mode: standalone` for small clusters. | bring your own (CloudNativePG, Qdrant chart, Valkey chart) | chart `modelPools` (vLLM / SGLang / TEI / llama.cpp), or your own llm-d / Dynamo | default-deny NetworkPolicy; allow-list per provider |
| **Air-gapped / sovereign** (`airgap/`) | regulated and classified sites | either of the above, fed from a signed bundle | same | open-weight only, pinned and hashed | none, by construction |

Ports: `8080` is the data plane (OpenAI-compatible `/v1/*`, `/healthz`, `/metrics`). `8081` is
the control plane (`/api/v1/*` and the web console at `/`).

## Quick start: single host (compose)

```bash
# 1. Image: build it (needs ../core and ../web) or load it from a bundle (airgap/README.md)
images/build.sh --version 0.1.0

# 2. Secrets
cd compose
cp .env.example .env && chmod 600 .env
#    fill in CALIBAN_ADMIN_TOKEN, CALIBAN_KEK, POSTGRES_PASSWORD, VALKEY_PASSWORD (see .env.example)

# 3. Weights into ./models (connected machine; licence-gated, pinned revisions)
../airgap/bundle.sh fetch --profile qwen3-small,embeddings,reranker --models-dir ./models
#    (fetch refuses unpinned revisions: pin models.lock.yaml first, or pass --allow-unpinned)

# 4. Start the pools for your hardware tier (table in "Running open models" below)
docker compose --profile qwen3-small --profile embeddings --profile reranker up -d   # 1×24 GB GPU
docker compose --profile cpu up -d                                                  # CPU only

# 5. Tenant API key, then smoke test
MINT_KEY=1 ../scripts/smoke.sh          # mints a key via the admin API and runs a chat completion
#    or offline: docker compose run --rm caliban keygen
#    and put the hash in config/caliban.toml -> tenants.api_key_hashes
```

The web console is at `http://127.0.0.1:8081/`. Log in with `CALIBAN_ADMIN_TOKEN`. By default
the ports bind to `127.0.0.1`. Put a TLS reverse proxy in front, or set `CALIBAN_BIND`.

**Network layout.**
- `postgres`, `qdrant`, `valkey` and every model server sit only on `backend`, which is
  `internal: true` and has no route off the host.
- `caliban` also joins `edge`, a bridge with IP masquerading disabled. That is enough to
  publish 8080/8081, but its containers cannot reach the internet.
- For a hard guarantee, add host firewall rules too (see the hardening checklist).

## Running open models

Every model runs in its own engine container (compose) or pool (Helm). Caliban sees each one
as a deployment-wide `[[providers]]` entry. Its `[[models]]` entries describe what the model
can do: tools, vision, how reasoning is switched, and context. The tenant routes then decide
which intent goes where. The full catalogue, with every repo ID and licence verified, is
`core/config/open-models.example.toml`. `compose/config/caliban.toml` is generated from it.
Background, parser flags and VRAM maths are in
`docs/research/09-open-models-on-prem.md`.

### Hardware tiers (estimates; benchmark per site)

| Tier | Chat | Embeddings | Reranker | Compose profiles |
|---|---|---|---|---|
| **CPU only** (≥ 32 GB RAM, 16+ cores) | Qwen3.6-35B-A3B Q4_K_M GGUF on llama.cpp (20.4 GB; 3B active) | Qwen3-Embedding-0.6B on TEI cpu | bge-reranker-v2-m3 on TEI cpu | `cpu` |
| **1 × 24 GB** (L4, A10, RTX 4090) | Qwen3.5-9B on vLLM, online FP8 (≈ 11 GB weights, 128k ctx); or gpt-oss-20b | Qwen3-Embedding-0.6B (TEI) | Qwen3-Reranker-0.6B (vLLM pooling) | `qwen3-small embeddings reranker` (or `gpt-oss …`) |
| **1 × 48 GB** (L40S, RTX 6000 Ada) | Qwen3.8-27B-FP8 (30.9 GB; 128k ctx with fp8 KV); throughput option: Qwen3.6-35B-A3B-FP8 | Qwen3-Embedding-0.6B | Qwen3-Reranker-0.6B | `qwen3-large embeddings reranker` |
| **1 × 80 GB** (H100, A100-80G) | Qwen3.8-27B-FP8 at 262k (`QWEN3_LARGE_MAX_MODEL_LEN=262144`); or gpt-oss-120b | Qwen3-Embedding-4B (Helm) or 0.6B | Qwen3-Reranker-4B (Helm) or 0.6B | `qwen3-large embeddings reranker` |
| **2–8 × 80 GB** | Qwen3.5-122B-A10B-FP8 (TP2), Qwen3.5-397B-A17B-FP8 (TP8) or DeepSeek-V4-Flash (MIT) + a 1-GPU Qwen3.8-27B fast tier | Qwen3-Embedding-8B | Qwen3-Reranker-8B | Helm `modelPools` (e.g. `qwen3-xl`) |

- **One embedder per deployment.** Its dimension (1024 / 2560 / 4096) is baked into the Qdrant
  collections. Changing the embedder means re-indexing.
- **Ollama** (`--profile ollama`) is a single endpoint serving many models
  (`qwen3.6:35b`, `qwen3-embedding:0.6b`, …). It suits small sites. It has no rerank endpoint,
  and its library tags cannot be pinned by revision.
- **Sharing a GPU.** Several pools can share one GPU (`*_GPU` picks the device), as long as the
  vLLM `*_GPU_UTIL` fractions plus about 1.5 GB for TEI fit on the card. The `.env.example`
  defaults fit `qwen3-small + embeddings + reranker` on 24 GB, and
  `qwen3-large + embeddings + reranker` on 48 GB.
- **Profile conflicts.** Do not combine `embeddings` and `cpu`: both serve the `embed` name.

### Adding a model

1. **Check the licence.** Look at `cardData.license` on Hugging Face. Apache-2.0 and MIT pass
   the bundle gate. Anything else needs legal review and `--allow-licence`.
2. **Add it to `airgap/models.lock.yaml`.** Give the repo ID, the pinned `revision`, the
   licence and a `dir`. Then fetch it on a connected machine:
   `airgap/bundle.sh fetch --model <repo-id> --pin`. This downloads with
   `hf download --revision`, records the tree sha256 and writes it into the lock.
3. **Serve it.** Copy one of the vLLM services in `compose/docker-compose.yml`, or a Helm
   `modelPools.pools` entry.
   - Set `--served-model-name` to the repo ID.
   - Add the right `--tool-call-parser` and `--reasoning-parser` for the family (table in note
     09 §3).
   - Embedders run on TEI or vLLM with `--runner pooling`. Rerankers run on TEI (XLM-R/GTE)
     or vLLM pooling.
4. **Declare it to Caliban.** Add a `[[providers]]` entry (the pool's `base_url`; set
   `cache_salt = true` only for vLLM/SGLang) and a `[[models]]` entry. Set
   `upstream_model = "<served name>"`, plus `kind`, `family`, `licence`, `context_window` (the
   served length) and `[models.capabilities]`: `tools`, `vision`, `reasoning`
   (`none|always|hybrid`), `reasoning_control` (`none|enable_thinking|reasoning_effort`) and
   `inline_think_tags`. Then reference the model in tenant routes.
5. **Restart and test.** Restart `caliban` and run
   `MODEL=<model id> MINT_KEY=1 scripts/smoke.sh`. Once the console's model discovery
   lands, steps 4–5 become "pick the endpoint, review the detected capabilities, save".

## Kubernetes (Helm)

Helm uses the existingSecret pattern for secrets. See `helm/caliban/README.md`.

```bash
kubectl create ns caliban && kubectl label ns caliban pod-security.kubernetes.io/enforce=restricted
kubectl -n caliban create secret generic caliban-auth \
  --from-literal=admin-token="$(openssl rand -hex 32)" --from-literal=kek="$(openssl rand -base64 32)"
kubectl -n caliban create secret generic caliban-database --from-literal=url='postgres://...'
helm install caliban helm/caliban -n caliban                         # connected / VPC
helm install caliban helm/caliban -n caliban -f helm/caliban/values-airgap.yaml   # air-gapped
```

## Air-gapped

See **[airgap/README.md](airgap/README.md)**. In short:

1. `bundle.sh` (connected side) saves the pinned images, copies the pinned weights, writes
   `MANIFEST.sha256` and signs it (cosign or minisign).
2. `load.sh` (site) checks the signature against a public key received out of band, verifies
   every file, then `docker load`s the images or pushes them to the site registry.

## Sizing rules of thumb

These are **estimates; benchmark per site.** The model-serving numbers come from
`docs/research/07-gateway-serving-and-sovereign-deployment.md` ("GPU sizing rules of thumb"
and "On-prem model serving recommendation").

**GPU memory for a model**

- **Weights ≈ params × bytes/param.** Bytes per param: FP16/BF16 = 2, FP8 = 1, INT4 ≈ 0.5–0.6.
- **KV cache per token ≈ 2 × layers × kv_heads × head_dim × bytes.** Example: a model with
  32 layers, 8 KV heads and head_dim 128 at FP16 needs 2·32·8·128·2 = 128 KiB per token.
  32 concurrent 8k-token sequences then need about 32 GiB of KV.
- **Reserve 20–40 % of HBM for KV** at your target concurrency. In compose, vLLM's
  `--gpu-memory-utilization` (per pool: `QWEN3_SMALL_GPU_UTIL`, `RERANK_GPU_UTIL`, …) sets the
  budget. Qwen3.5/3.6/3.8 keep KV only for 1 layer in 4 (the rest are DeltaNet layers), so
  their per-token KV is 20–64 KiB rather than the 128 KiB of the dense example above.

**Reference points**

- **gpt-oss-20b** fits a **16 GB** GPU.
- **gpt-oss-120b** fits **1 × 80 GB**.
- Frontier open MoE models (DeepSeek-V4-Pro, ~1.7T params) need multiple nodes with
  prefill/decode disaggregation (llm-d or NVIDIA Dynamo).

**Quantization:** FP8 on Hopper/Blackwell, AWQ INT4 on memory-bound sites, SmoothQuant W8A8
on Ampere.

**Engines:** vLLM by default; SGLang for agent/RAG pools with heavy prefix reuse; llama.cpp
for CPU, edge and dev. **Do not use TGI**: it has been in maintenance mode since December 2025
and was archived in March 2026.

**CPU profile (llama.cpp, Qwen3.6-35B-A3B Q4_K_M GGUF ≈ 20.4 GB, 3B active parameters):** plan
for 32 GB RAM or more (64 GB comfortable) and 16+ physical cores. Expect single-digit to low
double-digit tokens/s per stream. Use it for demos and low-volume internal use, not for
production SLOs. Smaller options: Qwen3.5-9B Q4_K_M (5.7 GB) or gpt-oss-20b MXFP4 (12.1 GB).

**Caliban itself:** the router adds little latency; the target is p50 < 3 ms and p99 < 10 ms,
excluding classifiers and RAG. The figures below are starting points only. Measure with your
own traffic before you commit to capacity.

| Component | Starting point |
|---|---|
| router | 0.5 vCPU / 512 MiB per replica, 2+ replicas, scale on CPU. SSE streams are long-lived, so scale down slowly. |
| control plane | 0.25 vCPU / 256 MiB |
| Postgres | 2 vCPU / 4 GiB; SSD; nightly `pg_dump` plus WAL archiving |
| Qdrant | RAM ≈ vectors × dim × 4 B × 1.5 (or on-disk / quantized collections); snapshots enabled |
| Valkey | 1 GiB, AOF on |

## Security hardening checklist

**Network**
- [ ] Datastores and model servers have no route out: compose `internal: true` backend, or the
      K8s default-deny NetworkPolicy.
- [ ] Host firewall backs up the container network config. In the `DOCKER-USER` chain, drop
      forwarded traffic from the Caliban bridges except to explicitly allowed provider ranges.
- [ ] Published ports bound to `127.0.0.1` or an internal NIC, with TLS terminated at your
      proxy or ingress. Enable mTLS for internal clients where possible.
- [ ] Control plane (8081) is **not** exposed to the same audience as the data plane: separate
      ingress or VPN only.
- [ ] No outbound DNS from Caliban hosts. Verify with firewall/DNS logs during a smoke test.

**Secrets and keys**
- [ ] `CALIBAN_KEK` is 32 random bytes, stored in a vault/HSM or a sealed secret, **backed up
      offline**, never in git or Helm values (`auth.create=false`).
- [ ] `CALIBAN_ADMIN_TOKEN` is 32+ random bytes, rotated on staff changes. Use OIDC once
      available.
- [ ] Postgres and Valkey have unique strong passwords. Postgres uses `scram-sha-256` and TLS
      (`sslmode=require`) when it runs off-host.
- [ ] `.env` is `chmod 600` and owned by the service account.

**Workloads**
- [ ] Caliban runs non-root (65532) with a read-only root FS, `cap_drop: ALL` and
      `no-new-privileges`. The namespace enforces the `restricted` Pod Security Standard.
- [ ] Images pinned by digest and loaded from a signed bundle or a private mirror. Verify image
      signatures and SBOMs (`build.sh --push --sbom`) in admission where supported.
- [ ] Model weights loaded read-only from pinned, hashed directories. `HF_HUB_OFFLINE=1`.
      vLLM runtime LoRA loading stays **off** on shared pools.
- [ ] Only Apache-2.0/MIT weights unless legal approved otherwise. Llama 4 is blocked for EU
      tenants. Qwen3.8-Flash-Next / Qwen3.8-2.4T (custom Qwen licences), Mistral Medium 3.5 /
      Devstral 2 123B (revenue cap) and MiniMax M2.7 (non-commercial) are not permissive.
- [ ] Shared vLLM/SGLang pools have `cache_salt = true` on their `[[providers]]` entry (tenant
      prefix-cache isolation). llama.cpp, Ollama and TEI pools serve one trust group only.

**Data**
- [ ] `security.egress = "deny_by_default"` stays in `caliban.toml`.
- [ ] PII mode `reversible` or `mask` is set for tenants handling personal data. ZDR is set
      where required.
- [ ] Telemetry: `OTEL_EXPORTER_OTLP_ENDPOINT` is empty or points at your collector only.
      Content capture is opt-in.
- [ ] Backups (Postgres, Qdrant snapshots, KEK) are tested by restoring them to a scratch host.

## BYOK and egress

There are two ways to give Caliban a tenant's provider key.

1. **Admin console or API (recommended).** The key is encrypted at rest (AES-256-GCM under the
   tenant DEK, wrapped by `CALIBAN_KEK`) and is never returned.

   ```bash
   curl -X POST http://127.0.0.1:8081/api/v1/tenants/acme/provider-keys \
     -H "Authorization: Bearer $CALIBAN_ADMIN_TOKEN" -H 'Content-Type: application/json' \
     -d '{"kind":"openai","label":"OpenAI prod","provider_id":"openai",
          "base_url":"https://api.openai.com/v1","api_key":"sk-...","trust_tier":"t2_contracted"}'
   ```

2. **Config reference.** In `caliban.toml`, declare
   `api_key = { env = "ACME_OPENAI_API_KEY" }` under `[[tenants.providers]]`. Then supply the
   variable through `.env` (compose) or `providerKeys.existingSecret` (Helm).

Either way, the provider's model must be in the catalogue (`[[models]]`) and in the tenant's
routes. `security.egress = "deny_by_default"` makes Caliban refuse any `base_url` that isn't
declared.

**Then open the network path for that provider only:**

- **Compose.** Add the override; only the `caliban` container gets a route out:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.byok-egress.yml --profile qwen3-small up -d
  ```

  Restrict the `br-caliban-egr` bridge in `DOCKER-USER` to the provider's ranges, or send
  traffic through your egress proxy (`CALIBAN_HTTPS_PROXY`). Example rules are in the
  override file.
- **Kubernetes.** List the provider under `networkPolicy.egress.providers` with its `cidrs`.
  With Cilium, set `networkPolicy.cilium.enabled=true` and list `hosts`
  (e.g. `api.openai.com`) to get an FQDN allow-list.
- **Air-gapped.** No BYOK providers. Remove or keep commented every non-local provider.

Trust tiers (`t0_sovereign`, …, `t3_public`) on providers and models let tenant policy keep
sensitive traffic on local models even when a BYOK provider is configured.

## Upgrade and rollback

Caliban is expected to run database migrations on start-up (confirm per release notes from
`core`). **Assume migrations are forward-only.**
Before any upgrade:
- back up Postgres (`pg_dump -Fc`);
- take a Qdrant snapshot;
- confirm you have the KEK.

A rollback across a migration means restoring that backup.

**Compose**

```bash
docker compose exec postgres pg_dump -U caliban -Fc caliban > backup-$(date +%F).dump
docker compose stop qdrant && docker run --rm --entrypoint tar -v caliban_qdrant-data:/d:ro \
  -v "$PWD":/b postgres:17.11-bookworm -C /d -cf /b/qdrant-$(date +%F).tar . && docker compose start qdrant
# upgrade
sed -i 's/^CALIBAN_VERSION=.*/CALIBAN_VERSION=0.2.0/' .env
docker compose pull caliban     # or airgap/load.sh the new bundle
docker compose up -d caliban
../scripts/smoke.sh
# rollback
sed -i 's/^CALIBAN_VERSION=.*/CALIBAN_VERSION=0.1.0/' .env
docker compose up -d caliban    # if 0.2.0 migrated the schema: restore the dump first
```

**Helm**

```bash
helm upgrade caliban helm/caliban -n caliban -f my-values.yaml --set image.digest=sha256:<new>
kubectl -n caliban rollout status deploy/caliban-router
helm rollback caliban <REVISION> -n caliban        # restores the previous manifests + config
```

- Router rollouts use `maxUnavailable: 0`, and the PDB keeps at least one router serving.
- Config changes trigger a rollout through the `checksum/config` annotation.
- Upgrade the control plane first in split mode. Routers keep serving their last snapshot if
  the control plane is briefly down (fail-static).

**Air-gapped:** use the same steps with a new bundle. Keep the previous bundle as rollback
media until the new version is accepted.

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
docker run --rm -v "$PWD":/mnt -w /mnt koalaman/shellcheck:stable -x images/build.sh airgap/*.sh scripts/smoke.sh
docker buildx build --check -f images/caliban.Dockerfile ..
```
