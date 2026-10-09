# caliban Helm chart

This chart deploys the Caliban router (data plane, `:8080`) and control plane (admin API and
web console, `:8081`) from the single `caliban/caliban` image.

```bash
kubectl create ns caliban
kubectl label ns caliban pod-security.kubernetes.io/enforce=restricted
kubectl -n caliban create secret generic caliban-auth \
  --from-literal=admin-token="$(openssl rand -hex 32)" \
  --from-literal=kek="$(openssl rand -base64 32)"
kubectl -n caliban create secret generic caliban-database \
  --from-literal=url='postgres://caliban:<pw>@postgres.db.svc:5432/caliban?sslmode=require'
# mode=split with router.mode=snapshot (the default) also needs the snapshot keys:
eval "$(docker run --rm caliban/caliban:0.1.0 gen-signing-key | sed 's/ *#.*//')"
kubectl -n caliban create secret generic caliban-snapshot \
  --from-literal=signing-key="$CALIBAN_SNAPSHOT_SIGNING_KEY" \
  --from-literal=public-key="$CALIBAN_SNAPSHOT_PUBLIC_KEY" \
  --from-literal=router-token="$(openssl rand -hex 32)"
helm install caliban ./caliban -n caliban            # connected / VPC
helm install caliban ./caliban -n caliban -f caliban/values-airgap.yaml   # air-gapped
```

## Key values

| Value | Default | Notes |
|---|---|---|
| `mode` | `split` | `split`: router Deployment (stateless, HPA-ready) plus control-plane Deployment. `standalone`: one pod running both. |
| `router.mode` | `snapshot` | Split mode only. `snapshot`: routers poll signed config snapshots from the control plane. `static`: routers read `config` from the ConfigMap. See [Split mode](#split-mode-routers-and-the-control-plane). |
| `router.snapshot.controlPlaneUrl` | `""` | Defaults to `http://<release>-control-plane:<controlPlane.service.port>`. |
| `router.snapshot.pollIntervalSeconds` | `10` | `CALIBAN_SNAPSHOT_POLL_SECS`; core adds ±20% jitter. |
| `router.snapshot.cache.enabled` | `true` | Keeps the last good signed snapshot in an `emptyDir` (`CALIBAN_SNAPSHOT_CACHE`). |
| `snapshotKeys.existingSecret` | `caliban-snapshot` | Keys `signing-key`, `public-key`, `router-token`. Needed only with `router.mode=snapshot`. `snapshotKeys.create=true` is for dev only. |
| `snapshotKeys.routerExistingSecret` | `""` | Optional Secret with only `public-key` and `router-token`, read by the routers instead. |
| `global.imageRegistry` | `""` | Private mirror for every image. The repository path is kept. |
| `image.digest` | `""` | Pin by digest (recommended). Takes precedence over `image.tag`. |
| `auth.existingSecret` | `caliban-auth` | Keys `admin-token` and `kek` (base64, 32 bytes). `auth.create=true` is for dev only. |
| `database.existingSecret` | `caliban-database` | Key `url`: the Postgres DSN. |
| `providerKeys.existingSecret` | `""` | Every key becomes an env var, for `api_key = { env = "..." }` refs. Mounted on routers too, since refs resolve where the request is served. |
| `extraEnv` | `[]` | Added to every Caliban pod. `router.extraEnv`, `controlPlane.extraEnv` and `standalone.extraEnv` add env to one component only. |
| `tcpNodelay` | `true` | Sets `CALIBAN_TCP_NODELAY` (`1`/`0`) on every Caliban pod. Keep it on: on Linux, Nagle plus delayed ACK added 24 to 50 ms to the first streamed token (core `bench/RESULTS-aws-2026-10.md`). |
| `qdrant.url` / `valkey.url` / `valkey.existingSecret` | in-namespace defaults | Valkey holds the quotas shared by all routers (`config.limits.store: valkey`) |
| `valkey.passwordSecret` / `valkey.passwordKey` | `""` / `password` | Optional: Valkey password from its own Secret (`CALIBAN_VALKEY_PASSWORD`), when the URL has none |
| `config` | see `values.yaml` | Rendered verbatim to `caliban.toml`, with the same schema as `core/config/caliban.example.toml`. |
| `router.autoscaling.enabled` | `false` | Turns on the HPA (CPU, optionally memory). Replicas are then left to the HPA. |
| `networkPolicy.enabled` | `true` | Default-deny ingress and egress, plus an explicit allow-list. |
| `networkPolicy.egress.providers` | `[]` | BYOK allow-list. `cidrs` go into the standard NetworkPolicy; `hosts` need `networkPolicy.cilium.enabled=true`. |

## Split mode: routers and the control plane

With `mode: split`, the control plane owns the state (Postgres) and the routers serve
traffic. `router.mode` decides how the routers get their config.

**`snapshot` (default).** Core's signed-snapshot protocol
([core README, "Split mode"](https://github.com/thecalibanproject/core#split-mode)):

- Routers run `caliban router` with `CALIBAN_CONTROL_PLANE_URL` set, so they never read the
  config file and the ConfigMap is not mounted.
- Every `pollIntervalSeconds` they call `GET /api/v1/snapshot` with the router token. They get
  `{key_id, payload, signature}`, verify the Ed25519 signature against
  `CALIBAN_SNAPSHOT_PUBLIC_KEY`, validate the config, refuse a snapshot issued before the one
  they serve (anti-rollback), then swap it in. While nothing changes the control plane
  answers `304`.
- Tenants, API keys, BYOK credentials and routes created in the console or the admin API
  reach every router within about one poll interval. The `[server]`, `[security]`, `[cache]`,
  `[pii]`, `[limits]` and `[routing]` sections come from the control plane's `config` and ship inside the
  snapshot, so editing `config` rolls only the control plane.
- **Fail-static.** On any error (control plane down, bad signature, invalid or older
  snapshot) a router logs it and keeps serving its last good snapshot. With
  `router.snapshot.cache.enabled`, that snapshot is also written (mode 0600) to an `emptyDir`
  and re-verified on load, so a router container that restarts while the control plane is
  down still serves. A new pod has no cache: it waits for the control plane before it
  listens, and its startup probe fails if that takes longer than the probe budget.

Where the keys go:

| Env var | Pod | From |
|---|---|---|
| `CALIBAN_SNAPSHOT_SIGNING_KEY` | control plane | `snapshotKeys.existingSecret`, key `signing-key` (base64 32-byte Ed25519 seed) |
| `CALIBAN_ROUTER_TOKEN` | control plane and routers | key `router-token` (any random string; not the admin token) |
| `CALIBAN_SNAPSHOT_PUBLIC_KEY` | routers | key `public-key` (base64; a comma-separated list is accepted for rotation) |
| `CALIBAN_KEK` | all | `auth.existingSecret`; routers open sealed BYOK credentials and derive the tenant `cache_salt` with it |
| `CALIBAN_ADMIN_TOKEN`, `CALIBAN_DATABASE_URL` | control plane, standalone | never on routers; core reads them only for the control plane |

`caliban gen-signing-key` prints a matching `CALIBAN_SNAPSHOT_SIGNING_KEY` and
`CALIBAN_SNAPSHOT_PUBLIC_KEY` pair. Pods read these Secrets at start only, and the chart does
not checksum Secrets, so restart the Deployments yourself after changing them
(`kubectl rollout restart deploy/<release>-router`). To rotate the signing key:

1. Set `public-key` to `<old>,<new>` and restart the routers.
2. Set `signing-key` to the new seed and restart the control plane.
3. Set `public-key` to `<new>` and restart the routers.

**`static`.** Routers run `caliban router` against the ConfigMap, like a file-driven
deployment. Changes made in the console or the admin API stay on the control plane, so keep
tenants, keys and routes in `config`. No snapshot Secret is needed.

### How `config` is rendered to TOML

- A map becomes a `[table]`.
- A list of maps becomes an `[[array.of.tables]]`.
- `{env: X}` and `{file: X}` become inline secret references.
- Scalars and lists of scalars become `key = value`.
- Strings are written with JSON escapes, which TOML reads the same way. For a newline (as in
  `routing.query_prefix` for Qwen3-Embedding), write `\n` inside a double-quoted YAML string;
  in a single-quoted or plain YAML string it stays a literal backslash and `n`.

Helm parses every YAML number as a float, so whole numbers are written as integers.
`0.0` becomes `0`, which serde accepts for `f64` fields.

## Security defaults

Every pod runs with:
- `runAsNonRoot` as uid 65532;
- `readOnlyRootFilesystem` (a 64 Mi `emptyDir` is mounted at `/tmp`);
- all capabilities dropped and `allowPrivilegeEscalation: false`;
- `seccompProfile: RuntimeDefault`;
- no service-account token;
- `enableServiceLinks: false`.

These meet the `restricted` Pod Security Standard.

NetworkPolicy:
- Egress is allowed only to cluster DNS, the other Caliban pods, the datastores, the local
  model servers, and any `providers` you list.
- Leaving `providers` empty means the pods have no internet path at all.

## Datastores: bring your own

Postgres, Qdrant and Valkey are **not** bundled as subcharts. Production sites run them
separately (managed service or operator), and the chart only needs URLs and Secrets. Our
recommendations, all licensed for on-prem redistribution:

| Store | Recommended | Wire it with |
|---|---|---|
| Postgres 17 | [CloudNativePG](https://cloudnative-pg.io) operator (Apache-2.0) | `database.existingSecret=<cluster>-app`, `database.urlKey=uri` |
| Qdrant | official `qdrant/qdrant` chart (Apache-2.0). Set `QDRANT__TELEMETRY_DISABLED=true`. | `qdrant.url=http://qdrant.<ns>.svc:6333` |
| Valkey | official `valkey` chart (BSD-3) | `valkey.existingSecret` with the `redis://:pw@host:6379/0` URL |

Avoid Bitnami charts and images for air-gapped installs. Since 2025 most Bitnami images are
no longer freely published, which breaks offline mirroring.

If you want them in the same release, vendor the charts into `charts/` and uncomment the
`dependencies` block in `Chart.yaml`. The entries are disabled by default via `condition`.
Remember to add their images to `airgap/images.lock`.

## Model serving

Two options. You can mix them.

**1. In-release model pools (`modelPools`).** The chart renders one Deployment and one Service
for each enabled entry in `modelPools.pools`. A pool is any OpenAI-compatible engine image
serving one model: vLLM, SGLang, TEI or llama.cpp. `values.yaml` ships disabled examples:

| Pool | What it serves | Hardware |
|---|---|---|
| `qwen3-small` | Qwen3.5-9B, online FP8 | 1 × 24 GB GPU |
| `qwen3-large` | Qwen3.8-27B-FP8 | 1 × 48 GB GPU, or 1 × 80 GB at 262k context |
| `qwen3-moe` | Qwen3.6-35B-A3B-FP8 | same as `qwen3-large` |
| `qwen3-xl` | Qwen3.5-122B-A10B-FP8 on SGLang, TP2 | 2 × 80 GB |
| `gpt-oss` | gpt-oss-20b | 1 GPU with 16 GB or more |
| `embed` | Qwen3-Embedding-0.6B on TEI | 1 small GPU (8 GB or more) |
| `rerank` | Qwen3-Reranker-0.6B on vLLM's pooling runner | 1 small GPU (8 GB or more) |
| `qwen3-cpu` | Qwen3.6-35B-A3B Q4_K_M on llama.cpp | CPU only |

How pools behave:
- **Naming.** The Service is named after the pool, so `config.providers[].base_url` is
  `http://<pool>:<port>/v1`. The pools in `values-airgap.yaml` and `config.providers` already
  match.
- **Weights.** Weights come from a PVC with the bundle's `models/` layout:
  `modelPools.defaults.weights.existingClaim`, or `weights.create=true` to get one PVC per pool.
  The PVC is mounted read-only at `/models`. `HF_HUB_OFFLINE=1` and the vLLM/HF telemetry
  opt-outs are set.
- **Network.** Pools fall under the release's default-deny NetworkPolicy and get **no egress
  rule at all**. Ingress is allowed only from the router/standalone pods
  (`<release>-ingress-model-pools`). The Caliban egress policy gets a matching rule
  automatically.
- **Security.** Pods run as non-root (uid 1000, all capabilities dropped, seccomp
  `RuntimeDefault`), which is compatible with the `restricted` Pod Security Standard. Caches go
  to an emptyDir at `/cache`.
- **GPUs.** `gpus: N` sets the `nvidia.com/gpu` limit. GPU pools get the
  `nvidia.com/gpu:NoSchedule` toleration. The rollout strategy is `Recreate`, so no spare GPU
  is needed during a rollout.
- **Mirrors.** With `global.imageRegistry`, the source registry host is dropped
  (`ghcr.io/x/y` → `<mirror>/x/y`), which matches `airgap/load.sh --registry`.

Engine flags (tool-call and reasoning parsers, `--hf-overrides` for Qwen3-Reranker) and VRAM
sizing per tier are explained in `docs/research/09-open-models-on-prem.md`.

**2. Engines you run yourself.** Use vLLM, SGLang, llm-d or NVIDIA Dynamo in their own
namespace, labelled e.g. `kubernetes.io/metadata.name: llm`. Then:
- point `config.providers[].base_url` at that engine;
- list it in `networkPolicy.egress.localModels` so the Caliban pods may reach it.

On air-gapped clusters, give the engine `HF_HUB_OFFLINE=1`, a pre-filled model volume and
(for gpt-oss) `TIKTOKEN_ENCODINGS_BASE`.
