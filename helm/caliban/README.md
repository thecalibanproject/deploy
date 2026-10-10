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
| `usageSpool.enabled` / `usageSpool.sizeLimit` | `true` / `512Mi` | Undelivered usage events of snapshot routers and standalone, in an `emptyDir` (`CALIBAN_USAGE_SPOOL_DIR`). See [Split mode](#split-mode-routers-and-the-control-plane). Off: they wait in memory and are lost when the container stops. |
| `snapshotKeys.existingSecret` | `caliban-snapshot` | Keys `signing-key`, `public-key`, `router-token`. Needed only with `router.mode=snapshot`. `snapshotKeys.create=true` is for dev only. |
| `snapshotKeys.routerExistingSecret` | `""` | Optional Secret with only `public-key` and `router-token`, read by the routers instead. |
| `global.imageRegistry` | `""` | Private mirror for every image. The repository path is kept. |
| `image.digest` | `""` | Pin by digest (recommended). Takes precedence over `image.tag`. |
| `auth.existingSecret` | `caliban-auth` | Keys `admin-token` and `kek` (base64, 32 bytes), and `oidc-client-secret` with single sign-on. `auth.create=true` is for dev only. |
| `sso.enabled` | `false` | Single sign-on for the console and admin API, with roles. See [Single sign-on](#single-sign-on). |
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
| `networkPolicy.egress.identityProvider` | `[]` | With `sso.enabled`: egress from the control plane to the issuer (`to` and `ports`, like `datastores`). |

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
- **Router id.** Each router checks in with `CALIBAN_ROUTER_ID`, which the chart sets to the
  pod name (downward API). The control plane records it for `caliban keys status`.
- **Usage shipping.** Routers send their usage events to the control plane
  (`POST /api/v1/usage/ingest`, router token), so `GET /api/v1/usage` and billing cover all
  router traffic. While the control plane is down, or refuses them (an older control plane
  answers 404), events wait in a spool at `/var/lib/caliban/usage/spool`
  (`CALIBAN_USAGE_SPOOL_DIR`, an `emptyDir` of `usageSpool.sizeLimit`) and are delivered once
  it answers; the control plane counts each event once. The `emptyDir` survives container
  restarts, not the pod: a pod deleted while the control plane is unreachable loses what is
  still spooled after its 5 s shutdown flush, so do not scale down or roll routers during a
  control-plane outage. Routers stay a Deployment rather than a StatefulSet with a PVC per
  pod: that would make rollouts serial, pin each router to its volume's zone, and still strand
  the spool of a pod removed by an HPA scale-down. Standalone gets the same spool for its
  shipping to Postgres.

Where the keys go:

| Env var | Pod | From |
|---|---|---|
| `CALIBAN_SNAPSHOT_SIGNING_KEY` | control plane | `snapshotKeys.existingSecret`, key `signing-key` (base64 32-byte Ed25519 seed) |
| `CALIBAN_ROUTER_TOKEN` | control plane and routers | key `router-token` (any random string; not the admin token) |
| `CALIBAN_SNAPSHOT_PUBLIC_KEY` | routers | key `public-key` (base64; a comma-separated list is accepted for rotation) |
| `CALIBAN_KEK` | all | `auth.existingSecret`; wraps the per-tenant data keys; routers open sealed BYOK credentials and derive the tenant `cache_salt` with it |
| `CALIBAN_KEK_PREVIOUS` | all | optional key `kek-previous` of `auth.existingSecret` (`auth.kekPreviousKey`): retired KEKs, only during a KEK rotation |
| `CALIBAN_ADMIN_TOKEN`, `CALIBAN_DATABASE_URL` | control plane, standalone | never on routers; core reads them only for the control plane. With `sso.enabled` the admin token key is optional (no key: break-glass access is off) |
| `CALIBAN_OIDC_CLIENT_SECRET` | control plane, standalone | with `sso.enabled` (not `sso.publicClient`): key `oidc-client-secret` of `auth.existingSecret` (`auth.oidcClientSecretKey`) |

`caliban gen-signing-key` prints a matching `CALIBAN_SNAPSHOT_SIGNING_KEY` and
`CALIBAN_SNAPSHOT_PUBLIC_KEY` pair. Pods read these Secrets at start only, and the chart does
not checksum Secrets, so restart the Deployments yourself after changing them
(`kubectl rollout restart deploy/<release>-router`). To rotate the signing key:

1. Set `public-key` to `<old>,<new>` and restart the routers.
2. Set `signing-key` to the new seed and restart the control plane.
3. Set `public-key` to `<new>` and restart the routers.

To rotate the KEK (core README, "KEK rotation"):

1. Set `kek` to a new key (`caliban gen-kek`) and `kek-previous` to the old one, then restart
   the routers, then the control plane.
2. `kubectl exec deploy/<release>-control-plane -- caliban keys rotate` (re-wraps every tenant
   data key and re-seals shared provider keys; audited).
3. When `kubectl exec deploy/<release>-control-plane -- caliban keys status` shows
   `"previous_keks_still_needed": []` and the routers have polled once, remove `kek-previous`
   and restart everything.
4. Keep the old key offline only as long as you keep backups taken before the rotation, then
   destroy it. Without it, those backups' BYOK keys and datasource credentials cannot be opened
   (for any tenant), which is also what makes a deleted tenant's keys unrecoverable from them.

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

## Single sign-on

The console and admin API can sign people in against your own OpenID Connect provider
(Keycloak, Entra ID, Okta, ADFS, Authentik, Dex and others), with roles: `owner`, `admin` and
`auditor` for the whole deployment, `tenant_admin`, `developer`, `viewer` and `billing` per
tenant. The control plane runs the login as backend for the console: tokens never reach the
browser, which holds only an `HttpOnly` session cookie. Provider setup (Keycloak and Entra ID
steps), roles and the permission of every admin route: core
[`docs/sso.md`](https://github.com/thecalibanproject/core/blob/main/docs/sso.md).

1. Register a confidential client at the provider with the redirect URI
   `https://<console host>/auth/callback`, the host your ingress serves the control plane on.
2. Add the client secret to the auth Secret:
   `kubectl create secret generic caliban-auth ... --from-literal=oidc-client-secret='<secret>'`
   (or patch the existing Secret).
3. Set the values and let the control plane reach the issuer:

   ```yaml
   sso:
     enabled: true
     issuer: https://keycloak.example.internal/realms/caliban   # exactly as discovery reports it
     clientId: caliban-console
     redirectUrl: https://caliban.example.internal/auth/callback
     groupsClaim: groups            # Keycloak realm roles: realm_access.roles; Entra app roles: roles
     apiAudience: caliban-api       # optional: access tokens for CI and scripts
     roleMappings:
       - { group: caliban-owners, role: owner }
       - { group: acme-developers, role: developer, tenant: acme }
   networkPolicy:
     egress:
       identityProvider:
         - to: [{ namespaceSelector: { matchLabels: { kubernetes.io/metadata.name: keycloak } } }]
           ports:
             - { port: 8443, protocol: TCP }
   ```

The chart renders these into `[security.oidc]` of `caliban.toml` (`client_secret = { env =
"CALIBAN_OIDC_CLIENT_SECRET" }`) and checks the role mappings. Other settings:
`sso.scopes`, `sso.caFile` (CA bundle for an internal PKI, mounted with `extraVolumes` and
`extraVolumeMounts`), `sso.postLogoutRedirectUrl`, `sso.sessionTtlSeconds`,
`sso.sessionIdleSeconds` and `sso.publicClient` (PKCE only, no secret). You can write
`config.security.oidc` yourself instead, with core's key names, but not both.

More roles are granted in the console (**Users and roles**), stored in Postgres. With several
control-plane replicas, sessions are shared through Postgres too.

The admin token stays the break-glass credential (owner rights, every use logged and audited).
Once an owner can sign in with SSO, keep it offline or set `sso.breakGlass: false` to refuse
it. With `sso.enabled`, the `admin-token` key of the auth Secret is optional.

Serve the console over https: the redirect URL's origin is checked against the browser's
`Origin` on writes, and an `https` redirect URL makes the session cookie `Secure` with the
`__Host-` prefix. With Cilium, `identityProvider` takes standard peers only (`ipBlock`,
selectors); for an FQDN rule use `networkPolicy.egress.extra` or a policy of your own.

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
  model servers, and any `providers` you list. With `sso.enabled`, the control plane can also
  reach the `identityProvider` peers.
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
| `qwen3-large` | Qwen3.8-27B-FP8 | 1 × 48 GB GPU (32k context when it shares the GPU with `embed` and `rerank`), or 1 × 80 GB at 262k context |
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
  rule at all**, except cluster DNS and the waited-for pools for a pool with `waitFor` (below). Ingress is allowed only from the router/standalone pods
  (`<release>-ingress-model-pools`). The Caliban egress policy gets a matching rule
  automatically.
- **Security.** Pods run as non-root (uid 1000, all capabilities dropped, seccomp
  `RuntimeDefault`), which is compatible with the `restricted` Pod Security Standard. Caches go
  to an emptyDir at `/cache`.
- **GPUs.** `gpus: N` sets the `nvidia.com/gpu` limit. GPU pools get the
  `nvidia.com/gpu:NoSchedule` toleration. The rollout strategy is `Recreate`, so no spare GPU
  is needed during a rollout.
- **Start order (`waitFor`).** Kubernetes has no start order between Deployments, so a pool
  can list other pools in `waitFor`: an init container, in the pool's own image (it needs
  `python3`, which the vLLM and SGLang images have), polls their health endpoints through
  their Services and starts the engine once all answer 200, or after `waitTimeoutSeconds`
  (default 900) in any case, so a broken embedder delays the chat pool but cannot keep it
  down. Pools that are disabled are skipped; a name that is not in `modelPools.pools` fails
  the render. The chart adds a NetworkPolicy per waiting pool that allows cluster DNS and the
  waited-for pools' port, and nothing else. In `values.yaml` the vLLM chat pools wait for
  `embed` and `rerank`. This matters when they share a GPU (time-slicing or MPS): vLLM sizes
  its KV cache from a memory profile taken at start, and in the second AWS run a 27B start
  that profiled while the reranker was loading got 1.21 GiB of KV cache instead of 5.15 GiB
  and crash-looped (core `bench/RESULTS-aws-2026-10b.md`). With whole GPUs per pool it only
  delays the chat pool by the small models' load time; set `waitFor: []` to skip it.
- **One 48 GB GPU for chat, embed and rerank.** The example pools assume a GPU each. To share
  one 48 GB card the way compose does, use the budget measured on an L40S: `qwen3-large` with
  `--gpu-memory-utilization=0.82` and `--max-model-len=32768` (and `context_window: 32768`
  for `local/qwen3.8-27b` in `config.models`), `rerank` with `--gpu-memory-utilization=0.10`,
  and about 1.5 GB for TEI. At 131072 one full-length sequence needs 4.31 GiB of a KV cache
  of about 5 GiB, too little margin to start reliably; 32768 refuses longer requests in
  exchange. Keep `waitFor: [embed, rerank]` on the chat pool.
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

## PII NER

The L1 NER detector runs in the pods that serve traffic (router, or standalone). In the second
AWS run it was 1.8 times faster per call on x86 with AVX-512 VNNI than on Graviton4 (43 ms
against 77 ms; 29.7 ms and 103 req/s on 8 vCPU with 2 threads per session, core
`bench/RESULTS-aws-2026-10b.md`), so pin those pods to amd64 nodes for NER-heavy tenants
(`router.nodeSelector: { kubernetes.io/arch: amd64 }`). The chart sets no `CALIBAN_PII_NER_*`
variables, so core's defaults apply; to override them, set both `CALIBAN_PII_NER_SESSIONS` and
`CALIBAN_PII_NER_THREADS` in `router.extraEnv`.
