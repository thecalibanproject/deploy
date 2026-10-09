# Air-gapped installation

This is the full offline procedure for Caliban's **sovereign / air-gapped** SKU: no outbound
network at install time or at runtime, and signed bundles carried across the air gap.

```
 CONNECTED SIDE (build/bastion host)                 AIR-GAPPED SITE
 ───────────────────────────────────                 ──────────────────────────────────
 1 build caliban image (optionally offline)          5 verify outer sha256
 2 download + pin model weights                      6 load.sh: verify signature, verify
 3 bundle.sh: save images, copy weights,               every file, docker load / push to
   MANIFEST.sha256, sign                               the site registry, copy weights
 4 transfer caliban-bundle-<v>.tar (+ .sha256)  ───► 7 deploy with compose or Helm
   on approved media; send the public key            8 smoke test
   through a separate channel
```

## Contents

| File | Purpose |
|---|---|
| `images.lock` | Pinned images (`groups  env-var  ref`). `core` is always bundled; other lines list the compose profiles that use the image (vLLM, TEI GPU/CPU, llama.cpp, Ollama). |
| `models.lock.yaml` | Model weights per hardware tier: source, pinned revision, licence, directory, compose profiles, tree sha256. Only **Apache-2.0 / MIT** by default. |
| `bundle.sh` | Connected side. `bundle.sh fetch` downloads the pinned weights (licence-gated) and records their tree sha256. `bundle.sh` (default mode) produces `caliban-bundle-<version>.tar` + `.tar.sha256`. |
| `load.sh` | Air-gapped side: verifies and loads a bundle. |

Bundle layout (inside the tar):

```
caliban-bundle-<v>/
  VERSION
  images/*.tar             docker save output, one per image
  images.txt               env-var  ref  tarfile  image-id  repo-digest
  models/<dir>/...         weights, laid out exactly as compose's $MODELS_DIR
  models.lock.yaml
  deploy/                  this repo: compose/, helm/, airgap/, scripts/, README.md
  MANIFEST.sha256          sha256 of every file above
  MANIFEST.sha256.sig      cosign signature   (or MANIFEST.sha256.minisig for minisign)
```

## Licence policy for models

- The default bundle contains only `apache-2.0` and `mit` weights. That covers Qwen3.5-9B,
  Qwen3-Embedding-0.6B, Qwen3-Reranker-0.6B, the Qwen3.6-35B-A3B GGUF, bge-reranker-v2-m3 and
  gpt-oss-20b. Optional entries (`bundle: false`, select them with `--model`) cover larger tiers:
  Qwen3.8-27B-FP8, Qwen3.6-35B-A3B-FP8, Qwen3.5-122B/397B-FP8, DeepSeek-V4-Flash (MIT) and
  gpt-oss-120b.
- `bundle.sh` **refuses** any other licence unless you pass `--allow-licence <id>`. Only do
  that after a legal review for the specific customer.
- **Llama 4 is excluded.** Its licence excludes EU-domiciled entities from the multimodal grant
  and adds a 700M-MAU clause and branding duties.
- **Other licences that are not permissive:**
  - Gemma 3 and EmbeddingGemma use custom Gemma terms. Gemma 4 itself is Apache-2.0.
  - Qwen3.8-Flash-Next and Qwen3.8-2.4T-A95B use custom Qwen licences with MaaS clauses.
    Qwen3.8-27B is Apache-2.0.
  - Mistral Medium 3.5 and Devstral 2 123B use a "Modified MIT" licence with a revenue cap.
  - MiniMax M2.7 is non-commercial.
  - The Jina v5 embedder and Jina v3 reranker are CC-BY-NC.
- See `docs/research/09-open-models-on-prem.md` §2 and the header of `models.lock.yaml`.

## 1. Build the Caliban image (connected side)

```bash
# Normal build (pulls crates/npm from the internet):
deploy/images/build.sh --version 0.1.0

# Hermetic build: vendor everything first, then build with --network=none.
deploy/images/build.sh vendor
deploy/images/build.sh --offline --version 0.1.0
```

The base images (`node:24-slim`, `rust:1-trixie`, `gcr.io/distroless/cc-debian13:nonroot`)
must be present or mirrored when building offline. Pin them by digest with
`--build-arg RUST_IMAGE=...@sha256:...` for reproducible builds.

## 2. Download and pin model weights (connected side)

`bundle.sh fetch` reads `models.lock.yaml` and does the following:
1. Applies the licence gate.
2. Downloads every selected entry with the Hugging Face CLI (`pip install -U huggingface_hub`)
   using `hf download <repo> --revision <commit> --local-dir <models-dir>/<dir>`, plus the
   entry's `--include` / `--exclude` globs. It falls back to `huggingface-cli download`.
3. Computes each directory's **tree sha256** and appends it to `<models-dir>/models.sha256`.
   With `--pin`, it also writes the hash into `models.lock.yaml`.

```bash
M=deploy/compose/models          # or any directory; pass it as --models-dir

# 1. Pin: copy the reviewed commit (the "# main@2026-10-02" comment is a starting point)
#    into each entry's `revision:` in deploy/airgap/models.lock.yaml.

# 2. Fetch the default bundle for the profiles you will run:
deploy/airgap/bundle.sh fetch --profile qwen3-small,embeddings,reranker,gpt-oss --models-dir "$M" --pin
deploy/airgap/bundle.sh fetch --profile cpu --models-dir "$M" --pin            # CPU-only sites
# Optional tiers (bundle: false) are selected one by one:
deploy/airgap/bundle.sh fetch --model Qwen/Qwen3.8-27B-FP8 --models-dir "$M" --pin
```

- **Pinning.** `fetch` refuses `TODO-pin-commit` revisions. `--allow-unpinned` fetches `main`
  for a trial run, but don't bundle that.
- **Licences.** It refuses any licence outside `apache-2.0` / `mit` unless you pass
  `--allow-licence <id>`. The licence check runs **before** any download starts.
- **Ollama.** Ollama library models are not on Hugging Face. To use the `ollama` profile, run
  `OLLAMA_MODELS="$M/ollama" ollama serve` on the connected host, then
  `ollama pull qwen3.6:35b` and `ollama pull qwen3-embedding:0.6b`, and bundle with
  `--model ollama/library`. For sovereign sites, prefer importing the pinned GGUF with a
  `Modelfile` (`FROM /models/qwen3.6-35b-a3b-gguf/Qwen3.6-35B-A3B-Q4_K_M.gguf`), because
  Ollama tags cannot be pinned.

> Why `tiktoken/`? gpt-oss's harmony tokenizer downloads `o200k_base` at start-up unless
> `TIKTOKEN_ENCODINGS_BASE` points to a local copy. Without these files vLLM tries to reach the
> internet and fails on an air-gapped host. `fetch` downloads them (entry
> `openai/tiktoken-encodings`) and the compose file sets the variable for you.

`bundle.sh` prints the tree hash of every model it copies. Once a hash is pinned, it fails if
the weights on disk don't match. The tree hash ignores `hf download`'s `.cache/` bookkeeping
and is computed like this:

```bash
(cd "$M/gpt-oss-20b" && find . -type f ! -path './.cache/*' -print0 | LC_ALL=C sort -z \
   | xargs -0 sha256sum) | sha256sum
```

For stronger provenance, verify publisher signatures (OMS / Sigstore model signing) where
the publisher provides them, before bundling.

## 3. Create signing keys (once) and bundle

Pick **one** signer. Keep the private key on the build side only. Send the public key to the
customer through a separate channel (for example, printed in the delivery note, or by phone
fingerprint check). Never trust a public key that arrives inside the bundle.

```bash
# minisign (simple, offline-friendly)
minisign -G -p caliban-bundle.pub -s caliban-bundle.key

# or cosign (key pair; no transparency log is used for offline bundles)
cosign generate-key-pair            # writes cosign.key / cosign.pub
```

Build the bundle:

```bash
deploy/airgap/bundle.sh --version 0.1.0 --profile qwen3-small,embeddings,reranker,gpt-oss \
  --models-dir deploy/compose/models --platform linux/amd64 \
  --sign minisign --key caliban-bundle.key --out ./dist
# -> dist/caliban-bundle-0.1.0.tar and dist/caliban-bundle-0.1.0.tar.sha256
```

`--profile` options (the same names as the compose profiles):
- `all` (the default): every image, and every `bundle: true` model.
- A comma-separated list: `qwen3-small`, `qwen3-large`, `qwen3-moe`, `gpt-oss`, `embeddings`,
  `reranker`, `cpu`, `ollama`, `k8s`. Only the images and `bundle: true` models those profiles
  use are included. `gpu` is kept as an alias for `qwen3-small,gpt-oss,embeddings,reranker`.
- `none`: Caliban and datastores only, for sites that already run their own model servers.
- `--model <id>` (repeatable): adds that `models.lock.yaml` entry even if it has
  `bundle: false`. Used without `--profile`, only the listed models (and the core images) are
  bundled.

Size: expect several tens of GB for the GPU profiles. `docker save` writes layers uncompressed
and the vLLM image is about 10 GB. The 24 GB tier's weights add about 35 GB: Qwen3.5-9B
19.3 GB, gpt-oss-20b 13.8 GB, embedder and reranker 1.2 GB each. The CPU tier adds about
24 GB. Make sure
`--out` has room for both the staging directory and the final tar (about 2× the bundle size).

## 4. Transfer

Copy the tar and its `.sha256` over your approved media or data diode. The tar is not
compressed on purpose: safetensors and GGUF don't compress, and this keeps verification streaming.

## 5–6. Verify and load (air-gapped side)

Requirements on the site: `docker` (or a host that can push to the site registry), `tar`,
`sha256sum`, and `minisign` or `cosign` matching the signer.

```bash
# Single docker host (compose):
deploy/airgap/load.sh caliban-bundle-0.1.0.tar \
  --pubkey caliban-bundle.pub \
  --models-dest /srv/caliban/models \
  --env-out /srv/caliban/images.env

# Kubernetes: push to the site registry instead (paths kept: <registry>/caliban/caliban, ...)
deploy/airgap/load.sh caliban-bundle-0.1.0.tar \
  --pubkey caliban-bundle.pub \
  --registry registry.internal:5000 \
  --env-out images.env
```

`load.sh` stops with an error in any of these cases:
- the outer `.tar.sha256` doesn't match;
- the signature is missing (unless you pass `--allow-unsigned`) or invalid;
- any file fails `MANIFEST.sha256`;
- the bundle contains a file the manifest doesn't list;
- the archive contains absolute or `..` paths.

To push without docker (e.g. from a hardened bastion), load the `images/*.tar` files with
`skopeo copy docker-archive:images/X.tar docker://registry/...` or `crane push`.

## 7. Deploy

**Compose (single host):**

```bash
cd caliban-bundle-0.1.0/deploy/compose
cp .env.example .env && chmod 600 .env     # fill in the secrets (see comments)
cat /srv/caliban/images.env >> .env        # image refs + MODELS_DIR from load.sh
docker compose --profile qwen3-small --profile embeddings --profile reranker up -d   # or --profile cpu
```

**Kubernetes:**

```bash
kubectl create ns caliban
kubectl label ns caliban pod-security.kubernetes.io/enforce=restricted
kubectl -n caliban create secret generic caliban-auth \
  --from-literal=admin-token="$(openssl rand -hex 32)" --from-literal=kek="$(openssl rand -base64 32)"
# KEK rotation later: add --from-literal=kek-previous=<old kek> next to the new kek, then
# `caliban keys rotate` (Helm README, "To rotate the KEK"). Back up every KEK offline.
kubectl -n caliban create secret generic caliban-pg-app --from-literal=uri='postgres://...'
kubectl -n caliban create secret generic caliban-valkey --from-literal=url='redis://:...@valkey:6379/0'
helm install caliban caliban-bundle-0.1.0/deploy/helm/caliban -n caliban \
  -f caliban-bundle-0.1.0/deploy/helm/caliban/values-airgap.yaml \
  --set global.imageRegistry=registry.internal:5000 \
  --set image.digest=sha256:<printed by load.sh>
```

`values-airgap.yaml` enables four `modelPools`: `qwen3-large`, `gpt-oss`, `embed` and
`rerank`. They read the weights from a PVC named `caliban-models` that holds the bundle's
`models/` tree. Create and fill that PVC before installing (for example with a loader Job, or
`kubectl cp` into a temporary pod). The pools already have `HF_HUB_OFFLINE=1` and
`TIKTOKEN_ENCODINGS_BASE`, have no egress, and are reachable only from Caliban. To use your own
llm-d or vLLM deployment instead, disable the pools and point the `base_url`s in
`values-airgap.yaml` at it.

## 8. Smoke test

```bash
deploy/scripts/smoke.sh                      # health only
MINT_KEY=1 deploy/scripts/smoke.sh           # mints a tenant key via the admin API, then chats
```

## Updates

Every update is a new bundle with the same procedure. Back up before loading it: Postgres dump,
Qdrant snapshot, and **the KEK**. Then load and deploy. See "Upgrade and rollback" in the
top-level README. Keep the previous bundle until the new version has been accepted, because
it is your rollback media.

## Verifying zero egress

- Compose: `docker compose exec qwen3-cpu curl -m 5 https://example.com` must fail (the backend
  network is `internal: true`). The caliban container has no shell. Check it from the host with
  `nsenter -t $(docker inspect -f '{{.State.Pid}}' caliban-caliban-1) -n curl -m 5 https://example.com`.
- Kubernetes: run a debug pod with the release's labels and try the same request. The default-deny
  `NetworkPolicy` must drop it.
- Watch the site firewall or DNS logs for any lookups from the Caliban hosts during a full
  smoke test. There should be none.
