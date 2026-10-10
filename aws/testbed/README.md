# Caliban AWS testbed

OpenTofu code for a short-lived AWS test environment in **eu-central-1 (Frankfurt)**. It runs the deploy repo's own compose stack, images and offline bundles on EC2 so that four things can be measured on real Linux hosts:

- (a) the gateway overhead benchmark;
- (b) split mode with N routers and a shared Valkey;
- (c) the GPU open-model tier: Qwen3.8-27B-FP8 on vLLM, Qwen3-Embedding-0.6B on TEI and Qwen3-Reranker-0.6B on one L40S 48 GB;
- (d) a zero-egress install from an offline bundle in a subnet with no route to the internet.

Nothing here is production guidance. Everything is off or small by default, every host stops itself after `ttl_hours`, and `tofu destroy` removes all of it.

The code holds no account ids, ARNs, IPs, keys or secrets. Account-specific values go in `terraform.tfvars`, which is git-ignored together with state and `.terraform/`.

## Contents

```
aws/
├── .gitignore                    state, tfvars, .terraform, backend.tf
└── testbed/
    ├── versions.tf               OpenTofu >= 1.8, pinned providers (.terraform.lock.hcl)
    ├── providers.tf              aws provider: region, profile, optional allowed_account_ids
    ├── variables.tf              every toggle, with defaults
    ├── locals.tf                 host map, AZ choice, fixed IPs, vCPU accounting
    ├── network.tf                VPC, public / private / isolated subnets, optional NAT
    ├── endpoints.tf              S3 gateway endpoint (policy-restricted), SSM interface endpoints
    ├── security.tf               host and endpoint security groups
    ├── iam.tf                    instance role: AmazonSSMManagedInstanceCore + bucket read
    ├── bucket.tf                 private bucket for bundles, tools and results
    ├── secrets.tf                generated secrets as SSM SecureString parameters
    ├── instances.tf              gateway, routers, loadgen, gpu (one for_each), cloud-init
    ├── autostop.tf               EventBridge Scheduler TTL stop
    ├── flowlogs.tf               optional VPC Flow Logs to CloudWatch Logs
    ├── checks.tf                 quota and sanity checks at plan time
    ├── outputs.tf                instance ids, SSM commands, bucket, TTL time
    ├── backend.s3.tf.example     optional S3 backend
    ├── terraform.tfvars.example
    ├── .tflint.hcl
    ├── tests/testbed.tftest.hcl  offline plan tests with a mocked AWS provider
    └── bootstrap/                what cloud-init runs on the hosts
        ├── common.sh             Docker, compose, image build or bundle load, secrets
        ├── gateway.sh            compose stack (standalone) + testbed override
        ├── router.sh             `caliban router` in split/snapshot mode
        ├── loadgen.sh            Rust, the bench crate, oha
        ├── gpu.sh                compose model profiles on the L40S
        ├── compose.testbed-gateway.yml
        └── compose.testbed-models.yml
```

## Topology

```
 VPC 10.42.0.0/16, one AZ (the first that offers g6e.xlarge and the CPU type)
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │ public 10.42.0.0/24          route 0.0.0.0/0 -> internet gateway             │
 │   hosts here: public IPv4, no inbound rule from outside the VPC              │
 │                                                                               │
 │ private 10.42.1.0/24         route 0.0.0.0/0 -> NAT only if enable_nat_gateway│
 │                                                                               │
 │ isolated 10.42.2.0/24        no internet route, ever                          │
 │   ssm, ssmmessages, ec2messages interface endpoints (private DNS)            │
 │   S3 gateway endpoint (also on the private route table)                      │
 └───────────────────────────────────────────────────────────────────────────────┘

 Hosts go in the tier given by network_mode (per role with role_network_mode),
 at fixed addresses in that /24:
   .10  gateway   Caliban standalone + Postgres + Valkey + Qdrant (compose)
   .20  gpu       vLLM / TEI model servers (compose profiles), ports 8000-8003
   .30  loadgen   bench load generator
   .40+ router-N  caliban router --control-plane-url http://<gateway>:8081
```

Access is **SSM Session Manager only**. There is no key pair, no SSH and no inbound port open to `0.0.0.0/0`. Security groups admit the service ports (8080, 8081, 5432, 6379, 6333-6334, 8000-8003, 9000-9001) from the VPC CIDR only.

## Prerequisites

1. **Tools on your machine:** OpenTofu 1.8 or later (`brew install opentofu`), AWS CLI v2 and the Session Manager plugin (`brew install --cask session-manager-plugin`).
2. **AWS CLI profile.** The provider uses the `default` profile (`aws_profile`). The account is shared with other workloads, so set `allowed_account_ids` in `terraform.tfvars` to stop an apply against the wrong account. Everything this module creates uses the `caliban-testbed` name prefix where a resource takes a name; no account-wide setting is changed (no default EBS encryption toggle, no account-level S3 Block Public Access change).
3. **Service quotas in eu-central-1.** These increases have **already been requested**. The commands are kept here for reference:

   | Quota | Code | Needed |
   |---|---|---|
   | Running On-Demand G and VT instances | `L-DB2E81BA` | 8 vCPU |
   | All G and VT Spot Instance Requests | `L-3819A6DF` | 8 vCPU |
   | Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances | `L-1216C47A` | 32 vCPU |

   ```bash
   aws service-quotas request-service-quota-increase --profile default --region eu-central-1 \
     --service-code ec2 --quota-code L-DB2E81BA --desired-value 8
   aws service-quotas request-service-quota-increase --profile default --region eu-central-1 \
     --service-code ec2 --quota-code L-3819A6DF --desired-value 8
   aws service-quotas request-service-quota-increase --profile default --region eu-central-1 \
     --service-code ec2 --quota-code L-1216C47A --desired-value 32

   # status
   aws service-quotas list-requested-service-quota-change-history --profile default \
     --region eu-central-1 --service-code ec2 \
     --query 'RequestedQuotas[].[QuotaName,DesiredValue,Status]' --output table
   ```

   With `cpu_use_spot = true`, the CPU hosts also need "All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests" (`L-34B43A08`). `checks.tf` warns at plan time when the enabled hosts exceed `standard_vcpu_quota` (32) or `gpu_vcpu_quota` (8). The defaults fit exactly: gateway 8 + loadgen 8 + 4 routers × 4 = 32 standard vCPUs, and the GPU host uses 4 of the 8 G vCPUs.

4. **g6e in Frankfurt.** The module picks the first AZ that offers both `g6e.xlarge` and the CPU type. To see which ones do before the first apply:

   ```bash
   aws ec2 describe-instance-type-offerings --profile default --region eu-central-1 \
     --location-type availability-zone --filters Name=instance-type,Values=g6e.xlarge,c8g.2xlarge
   ```

## Quick start

```bash
cd deploy/aws/testbed
cp terraform.tfvars.example terraform.tfvars     # optional; git-ignored
tofu init
tofu plan                                        # gateway + loadgen, public tier, build from main
tofu apply
tofu output ssm_sessions                         # shell on each host
```

Each host bootstraps itself with cloud-init and logs to `/var/log/caliban-testbed.log`. It writes `/var/lib/caliban-testbed/ready` when done, or `failed` on error:

```bash
aws ssm start-session --profile default --region eu-central-1 --target <gateway id>
sudo tail -f /var/log/caliban-testbed.log
```

A source build of the Caliban image takes roughly 15 to 25 minutes on the gateway. Pulling vLLM and the 33 GB of weights on the GPU host takes roughly 10 to 20 minutes.

The console and data plane can be reached from your laptop through SSM port forwarding (`tofu output console_port_forward`), then `http://127.0.0.1:8081/`. The admin token is in SSM: `aws ssm get-parameter --with-decryption --name /caliban-testbed/admin-token --query Parameter.Value --output text`.

## Toggles and what they create

Always created (free or nearly free while idle): the VPC, its three subnets, route tables, internet gateway, the locked-down default security group, the host and endpoint security groups, the S3 gateway endpoint, the bucket (with versioning, SSE-S3, per-bucket public access block, ownership controls, TLS-only policy and a lifecycle rule), the instance role and profile, and the SSM parameters with the generated secrets.

| Toggle | Default | Resources |
|---|---|---|
| `gateway_enabled` | `true` | `aws_instance.host["gateway"]` (c8g.2xlarge) |
| `router_count` | `0` | `aws_instance.host["router-1..N"]` (c8g.xlarge each) |
| `loadgen_enabled` | `true` | `aws_instance.host["loadgen"]` (c8g.2xlarge) |
| `gpu_enabled` | `false` | `aws_instance.host["gpu"]` (g6e.xlarge, DLAMI, 150 GB gp3; 300 GB when isolated) |
| `gpu_market` | `spot` | persistent spot request that stops on interruption; `on-demand` is the fallback |
| `cpu_use_spot` | `false` | the same spot options on the CPU hosts |
| any host enabled | | `time_static.ttl`, `aws_scheduler_schedule.ttl_stop`, `aws_iam_role.autostop` + policy |
| `network_mode`, `role_network_mode` | `public` | which subnet each host uses; public IPv4 only in `public` |
| `enable_nat_gateway` | `false` | `aws_eip.nat`, `aws_nat_gateway.this`, `aws_route.private_nat` |
| `enable_ssm_endpoints` | `null` (auto) | `aws_vpc_endpoint.ssm["ssm"\|"ssmmessages"\|"ec2messages"]`; auto = on while any host has no internet route |
| `enable_flow_logs` | `false` | `aws_flow_log.vpc`, `aws_cloudwatch_log_group.flow_logs`, `aws_iam_role.flow_logs` + policy |
| `s3_endpoint_allow_al2023_repos` | `true` | adds the Amazon Linux 2023 repository bucket to the S3 endpoint policy |
| `results_upload` | `false` | adds `s3:PutObject` on `results/*` to the instance role |
| `image_source` | `build` | `build`: clone and build on the host; `s3`: load a bundle (forced for isolated hosts) |
| `cpu_arch` | `arm64` | Graviton c8g; `x86_64` uses c7i |

Other useful variables: `core_ref` (a branch or tag to build), `web_ref`, `deploy_ref`, `caliban_version`, `gpu_compose_profiles`, `gpu_run_caliban`, `gateway_use_gpu_models`, `bench_ref`, `bench_package`, `ttl_hours`, `bucket_force_destroy`. See `variables.tf`.

**Why arm64 for the CPU hosts.** The Dockerfile's base images (`node:24-slim`, `rust:1-trixie`, `gcr.io/distroless/cc-debian13:nonroot`) are multi-arch, `build.sh` takes `--platform`, and the image already builds and runs on linux/arm64 with the `ner` feature (ONNX Runtime ships aarch64 binaries). Graviton is cheaper per vCPU. The GPU host is x86_64 regardless (g6e). Set `cpu_arch = "x86_64"` when one amd64 bundle should serve every host (scenario d with the GPU).

## What runs on each host

| Host | AMI | What the bootstrap does |
|---|---|---|
| gateway | Amazon Linux 2023 | Docker, compose and buildx plugins; image (build or bundle); `compose/.env` with secrets from SSM; `docker compose -f docker-compose.yml -f compose.testbed-gateway.yml up -d --wait` |
| router-N | Amazon Linux 2023 | Docker; image; `docker run caliban/caliban:<v> router --control-plane-url http://<gateway>:8081 --snapshot-cache ...` with the snapshot public key, router token, KEK and the gateway's Valkey |
| loadgen | Amazon Linux 2023 | build tools, rustup (stable), `cargo install oha`, core at `bench_ref` and `cargo build --release -p caliban-bench` if that package exists; else `s3://<bucket>/tools/<arch>/caliban-bench` |
| gpu | Deep Learning Base OSS NVIDIA Driver GPU AMI (Ubuntu 24.04), from the public SSM parameter | NVIDIA driver, Docker and NVIDIA Container Toolkit come with the AMI; weights with `airgap/bundle.sh fetch` (connected) or from the bundle (s3); `docker compose ... --profile qwen3-large --profile embeddings --profile reranker up -d qwen3-large embed rerank` |

`compose.testbed-gateway.yml` gives the gateway's Caliban the split-mode signing key and router token, swaps its `edge` network for a NAT'd bridge so it can call the GPU host and mock upstreams in the VPC, and publishes Postgres, Valkey and Qdrant on the host's private IP through a bridge with masquerading off. When the GPU host is enabled, the gateway's `caliban.toml` points the model providers at it (`8000` chat, `8001` embed, `8002` rerank) before the first start, because Postgres becomes the source of truth after that.

`compose.testbed-models.yml` publishes the model servers on the GPU host's private IP through a bridge with masquerading off, so they still have no route out.

The PII NER model is off in the testbed (`CALIBAN_PII_NER_DIR` empty). To test it, fetch the artifact with the ml repo's `scripts/fetch_pii_ner.py`, upload it, copy it to `/srv/caliban/models/pii` and set the variable in `compose/.env`.

## Images, bundles and tools in the bucket

- **`image_source = "build"`** (connected hosts): clones `core`, `web` and `deploy` from `github.com/thecalibanproject` side by side under `/opt/caliban/src` at `core_ref`, `web_ref` and `deploy_ref`, then runs `deploy/images/build.sh --version <caliban_version>`.
- **`image_source = "s3"`** (forced for isolated hosts): downloads `s3://<bucket>/bundles/<arch>/caliban-bundle-<v>.tar` and its `.tar.sha256`, and verifies and loads it with this repo's `airgap/load.sh` (the copy delivered by cloud-init, not the one inside the bundle). The public key comes from `bundle_pubkey`, which plays the role of the out-of-band key. The GPU host also gets the weights from the bundle (`--models-dest /srv/caliban/models`).

Build the bundles on a connected machine (see `airgap/README.md`):

```bash
B=$(tofu -chdir=deploy/aws/testbed output -raw bucket)

# CPU hosts (arm64): Caliban and datastores only. Builds natively on Apple silicon.
deploy/images/build.sh --version 0.1.0
deploy/airgap/bundle.sh --version 0.1.0 --profile none --platform linux/arm64 \
  --sign minisign --key caliban-bundle.key --out dist/arm64
aws s3 cp dist/arm64/ "s3://$B/bundles/arm64/" --recursive --exclude '*' --include 'caliban-bundle-0.1.0.tar*'

# GPU host (amd64): vLLM, TEI and the 48 GB tier's weights. bundle.sh always includes the
# Caliban image, so an amd64 build of it must exist locally (emulated on Apple silicon: slow).
deploy/images/build.sh --version 0.1.0 --platform linux/amd64
deploy/airgap/bundle.sh fetch --profile embeddings,reranker --model Qwen/Qwen3.8-27B-FP8 \
  --models-dir deploy/compose/models --allow-unpinned
deploy/airgap/bundle.sh --version 0.1.0 --profile qwen3-large,embeddings,reranker \
  --model Qwen/Qwen3.8-27B-FP8 --platform linux/amd64 --models-dir deploy/compose/models \
  --sign minisign --key caliban-bundle.key --out dist/amd64
aws s3 cp dist/amd64/ "s3://$B/bundles/amd64/" --recursive --exclude '*' --include 'caliban-bundle-0.1.0.tar*'
```

A local image store holds one platform per tag unless Docker uses the containerd image store, so build and bundle one architecture at a time (or use `cpu_arch = "x86_64"` and a single amd64 bundle). The GPU bundle is about 50 GB; S3 storage for it is about $1.20 a month.

**Building the bundle on a testbed host instead** (no local Docker build, native amd64 on a c7i): apply with `cpu_arch = "x86_64"`, the gateway's tier set to `isolated` in `role_network_mode` and `gateway_enabled = false`, so only the connected loadgen starts. On it, install Docker and buildx, clone core, web and deploy side by side, run `images/build.sh` and `airgap/bundle.sh --profile none --platform linux/amd64 --sign minisign` with a key generated there (`minisign -G -W`). The instance role may only write under `results/`, so copy the bundle, `docker-compose` and `minisign` to `results/stage/...` from the host, then server-side into place with your own credentials (`aws s3 cp s3://$B/results/stage/bundles/amd64/caliban-bundle-0.1.0.tar s3://$B/bundles/amd64/`, and the same for `.tar.sha256`, `tools/amd64/docker-compose` and `tools/amd64/minisign`). Put the public key in `bundle_pubkey` and set `gateway_enabled = true`: the loadgen keeps running. The second AWS run (core `bench/RESULTS-aws-2026-10b.md`) did this: 2 min 46 s for the image, an 846 MB bundle.

**Static tools for hosts without internet** go under `s3://<bucket>/tools/<arch>/` (`arm64` or `amd64`):

| File | Needed when |
|---|---|
| `docker-compose` | an isolated Amazon Linux host runs compose (gateway). From `github.com/docker/compose/releases` (`docker-compose-linux-aarch64` or `-x86_64`), renamed. |
| `minisign` or `cosign` | `bundle_verify` is `minisign` or `cosign` and the AMI does not have it |
| `caliban-bench` | an isolated loadgen should run the bench (build it on a connected loadgen or in CI) |

## Secrets and state

`secrets.tf` generates the admin token, KEK (32 random bytes), Postgres and Valkey passwords, the router token and an Ed25519 snapshot signing key. They are stored as SSM SecureString parameters under `/caliban-testbed/` (AWS managed `aws/ssm` key) and read by the hosts at boot. The instance role can read them because `AmazonSSMManagedInstanceCore` includes `ssm:GetParameter`; that also means every testbed host can read every testbed secret, which is acceptable for a testbed only. Routers derive the public key and never read the private one.

They are not in user data or in git, but they **are in the OpenTofu state**. State is local by default (`terraform.tfstate`, git-ignored). To keep it in S3 instead, create a versioned, encrypted state bucket by hand (not with this module, which would destroy it), copy `backend.s3.tf.example` to `backend.tf`, fill it in and run `tofu init -migrate-state`. It locks with an S3 lock file (`use_lockfile`), so no DynamoDB table is needed.

## Network: what the isolated subnet can reach

The isolated route table has the implicit VPC-local route and the S3 gateway endpoint's prefix-list route, nothing else. Isolated hosts get the `hosts` security group only (egress: VPC CIDR, and HTTPS to the S3 prefix list). Exactly this is reachable from there:

| Destination | How | Limit |
|---|---|---|
| other hosts in the VPC | local route | security groups: service ports from the VPC CIDR |
| `ssm`, `ssmmessages`, `ec2messages` in eu-central-1 | interface endpoints in the isolated subnet, private DNS | endpoint security group: 443 from the VPC |
| S3 in eu-central-1 | gateway endpoint | endpoint policy: the testbed bucket (get, put, list; principals of this account only; puts still need `results_upload`), and `s3:GetObject` only on the Amazon Linux 2023 repository bucket `al2023-repos-eu-central-1-de612dc2` (turn off with `s3_endpoint_allow_al2023_repos = false`) and the SSM Agent update buckets `amazon-ssm-eu-central-1` and `amazon-ssm-packages-eu-central-1`. dnf and the agent read those anonymously, so that statement has no account condition (an account condition made every package download fail with 403). No other bucket. |
| Amazon DNS resolver (VPC base + 2, 169.254.169.253) | always on in a VPC | not filtered by security groups |
| instance metadata (169.254.169.254) and Amazon Time Sync (169.254.169.123) | link-local | IMDSv2 only, hop limit 1 (containers cannot reach it) |

Nothing else: no internet gateway route, no NAT, no other AWS service endpoint (no EC2, STS, CloudWatch, ECR). The one residual channel is DNS: the Route 53 Resolver will still resolve public names, so DNS lookups for `example.com` succeed even though connections to the answer fail. Closing that needs Route 53 Resolver DNS Firewall, which this module does not set up.

Interface endpoints cost about **$0.012 per hour each** in eu-central-1 (three of them: about $0.036/h, about $26 a month if left up), plus $0.01 per GB processed. That is why `enable_ssm_endpoints` defaults to "on only while a host needs them". Public-tier hosts reach SSM over the internet gateway instead.

## Cost

Prices are **estimates for eu-central-1, on-demand, Linux, October 2026; verify them on the AWS pricing pages** (EC2 on-demand and spot, VPC, EBS) before relying on them. Spot prices move; check the current price in the EC2 console's spot price history.

| Component | On-demand / hour | Typical spot / hour | Notes |
|---|---|---|---|
| gateway c8g.2xlarge (8 vCPU, 16 GiB) | $0.3628 (price list, 2026-10-09) | ~$0.19 to 0.20 (2026-10-09) | c7i.2xlarge ~$0.42 |
| loadgen c8g.2xlarge | $0.3628 | ~$0.19 to 0.20 | |
| router c8g.xlarge (4 vCPU, 8 GiB), each | ~$0.19 | ~$0.07 to 0.09 | c7i.xlarge ~$0.21 |
| gpu g6e.xlarge (1 × L40S 48 GB) | $2.327 (price list, 2026-10-09) | $1.74 (1a), $2.07 (1c), $2.33 (1b) on 2026-10-09 | spot was close to on-demand; us-east-1 lists $1.861 on-demand |
| gp3 root volume | ~$0.013 per 100 GB | | ~$0.0952 per GB-month; billed while stopped |
| public IPv4 (public tier), each | $0.005 | | released while stopped |
| SSM interface endpoints (3) | ~$0.036 | | auto toggle |
| NAT gateway | ~$0.052 | | plus ~$0.052 per GB |
| VPC Flow Logs | per GB ingested, ~$0.57 | | small for these tests |
| S3 | ~$0.0245 per GB-month | | a 50 GB bundle ~$1.20/month |
| EventBridge Scheduler, SSM parameters, IAM, VPC, S3 gateway endpoint | ~$0 | | |

Inbound internet traffic (image pulls, Hugging Face downloads), S3 to EC2 in the same region and traffic between private IPs in one AZ are free.

**Per scenario** (on-demand CPU hosts, compute + volumes + IPv4, rounded):

| Scenario | Hosts | ~$/hour |
|---|---|---|
| (a) gateway overhead | gateway, loadgen | 0.77 |
| (b) split mode, 4 routers | gateway, loadgen, 4 routers | 1.57 (0.77 + 0.20 per router) |
| (c) GPU tier | gateway, loadgen, gpu | about 2.5 with GPU spot at $1.74; 3.1 with GPU on-demand |
| (d) zero egress, CPU only | gateway, loadgen (isolated) + 3 endpoints + flow logs | 0.80 |
| (d) zero egress with GPU | + gpu (isolated, 300 GB) | 1.8 to 2.1 spot; 3.1 on-demand |

Add the time spent bootstrapping (source build, weight download) to each run.

## Auto-stop and cost control

The cost controls are the TTL auto-stop and the teardown checklist below.

1. **EventBridge Scheduler (all hosts).** `aws_scheduler_schedule.ttl_stop` is a one-shot schedule at `ttl_hours` (default 4) after the current set of hosts was created. It calls `ec2:StopInstances` with the explicit instance ids, using a role that may stop exactly those instance ARNs and nothing else. `tofu output ttl_stop_at` shows when. It re-arms whenever the host set changes. To extend a run: `tofu apply -replace=time_static.ttl` (another `ttl_hours` from now).
2. **OS timer (on-demand hosts).** A systemd timer powers the OS off `ttl_hours` after every boot, and `instance_initiated_shutdown_behavior = "stop"` turns that into a stop. To extend on a host: `sudo systemctl stop caliban-ttl.timer`. Spot hosts skip it and rely on the scheduler (a persistent spot request with stop-on-interruption can be stopped through the API).

A stopped host still pays for its EBS volume (about $14 a month for the 150 GB GPU volume). A started host gets a fresh OS timer, but the scheduler fires only once: run `tofu apply -replace=time_static.ttl` after starting hosts by hand.

## Scenario runbooks

Shell variables used below: `P="--profile default --region eu-central-1"`. Run `tofu output` for ids and IPs. On the hosts, `/etc/profile.d/caliban-testbed.sh` exports `GATEWAY_IP`, `GPU_IP`, `LOADGEN_IP` (each role's planned address, set even when that host is not enabled) and `CALIBAN_DEPLOY_DIR`. Router N is at the router tier's `.40 + N - 1` (`tofu output hosts`).

Host user data holds no other host's state, so adding or removing a host (routers, the gateway, the loadgen) leaves the others running. The exception is the GPU: the gateway's model URLs depend on `gpu_enabled`, so toggling it replaces the gateway. To pause the GPU and keep the gateway, stop the GPU instance with `aws ec2 stop-instances` instead.

The scripts used for the measurement runs (real-model and `caliban/auto` benchmarks, semantic-cache pairs, billing, split-mode, single sign-on and console checks) are in core's `bench/scripts/`; they read these variables.

Common steps on the loadgen (admin token from SSM, a tenant key minted through the admin API):

```bash
ADMIN=$(aws ssm get-parameter --with-decryption --name /caliban-testbed/admin-token \
  --query Parameter.Value --output text)
KEY=$(curl -s -X POST -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"name":"bench"}' "http://$GATEWAY_IP:8081/api/v1/tenants/default/api-keys" | jq -r .key)
```

To keep results, set `results_upload = true` and copy them to `s3://<bucket>/results/<scenario>/`, then `aws s3 sync` them down to your laptop.

### (a) Gateway overhead bench on Linux

Goal: Caliban's added latency (design target p50 < 3 ms, p99 < 10 ms, without classifiers or RAG) and throughput, against a mock upstream that answers instantly.

```bash
tofu apply                                   # defaults: gateway + loadgen, public tier
```

1. Wait for `ready` on both hosts.
2. The bench crate (`caliban-bench`, with the `mock-upstream` binary) is on core's `main`. `loadgen.sh` builds it from `bench_ref` (`bench_package`), or installs a prebuilt binary from `s3://<bucket>/tools/<arch>/caliban-bench`; `oha` is installed too. The suite measures each scenario direct to the mock and through Caliban (see core's `bench/RESULTS.md`). To run it across hosts, build `caliban`, `mock-upstream` and `caliban-bench` on the loadgen (`cargo build --release -p caliban -p caliban-bench`), copy them to the gateway through `s3://<bucket>/results/` (needs `results_upload = true`), then:

   ```bash
   # gateway: mock and gateways on ports the security group admits (9000-9001, 8000-8002)
   CALIBAN_TCP_NODELAY=1 ./caliban-bench --caliban ./caliban --bind 0.0.0.0 --advertise "$GATEWAY_IP" \
     --mock-ports 9000,9001 --gateway-ports 8000,8001,8002 --serve ep.json
   # loadgen, with ep.json copied over
   ./caliban-bench --remote ep.json --concurrency 1,16,64 --out cross.md --json cross.json
   ```

   Without `--serve`/`--remote`, `caliban-bench --caliban ./caliban` runs everything on one host. Results of the first run: core `bench/RESULTS-aws-2026-10.md`.

   **PII NER builds.** The `ner` feature does not link on Amazon Linux 2023: the prebuilt ONNX Runtime that `ort` downloads needs GCC 13/14 libstdc++ and AL2023 ships GCC 11. Build and run that variant in a `rust:1-trixie` container, the toolchain of the Caliban image (Debian trixie, so the image itself is not affected). The binaries need trixie's glibc, so run them in the same container:

   ```bash
   # in the core checkout on the host
   sudo docker run --rm -it --network host -v "$PWD":/src -w /src \
     -e CARGO_TARGET_DIR=/src/target-trixie rust:1-trixie bash
   # inside the container
   cargo build --release -p caliban -p caliban-bench --features caliban/ner
   CALIBAN_PII_NER_DIR=/src/models/pii/<artifact>/<version> CALIBAN_TCP_NODELAY=1 \
     target-trixie/release/caliban-bench --caliban target-trixie/release/caliban --concurrency 1,16,64 --out ner.md
   ```

   The separate target directory keeps these builds apart from the AL2023 ones in `target/`.
3. Register the mock as a provider, a model and the tenant route (admin API on the gateway):

   ```bash
   H=(-H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json')
   curl -s "${H[@]}" -X POST "http://$GATEWAY_IP:8081/api/v1/providers" \
     -d "{\"id\":\"mock\",\"kind\":\"openai_compatible\",\"base_url\":\"http://$LOADGEN_IP:9000/v1\",\"trust_tier\":\"t0_sovereign\"}"
   curl -s "${H[@]}" -X POST "http://$GATEWAY_IP:8081/api/v1/models" \
     -d '{"id":"bench/mock","provider":"mock","upstream_model":"mock","kind":"chat","trust_tier":"t0_sovereign"}'
   curl -s "${H[@]}" -X PUT "http://$GATEWAY_IP:8081/api/v1/tenants/default/routes" \
     -d '{"routes":[{"intent":"default","models":["bench/mock"]}]}'
   ```

4. Baseline (direct to the mock), then through Caliban, same load:

   ```bash
   BODY='{"model":"mock","messages":[{"role":"user","content":"hello"}],"max_tokens":16}'
   oha -z 60s -c 64 -m POST -H 'Content-Type: application/json' -d "$BODY" \
     "http://$LOADGEN_IP:9000/v1/chat/completions"
   oha -z 60s -c 64 -m POST -H 'Content-Type: application/json' -H "Authorization: Bearer $KEY" \
     -d "${BODY/\"mock\"/\"bench/mock\"}" "http://$GATEWAY_IP:8080/v1/chat/completions"
   ```

   The difference in p50 and p99 is the gateway overhead. Repeat with PII mode `off`, `mask` and `reversible`, and with the exact cache on and off.

### (b) Split mode with N routers and a shared Valkey

```bash
tofu apply -var router_count=4               # 32 standard vCPUs with gateway + loadgen
```

1. The gateway runs `standalone` with `CALIBAN_SNAPSHOT_SIGNING_KEY` and `CALIBAN_ROUTER_TOKEN`, so it serves signed snapshots at `/api/v1/snapshot`. Each router polls it every 10 s and points `CALIBAN_VALKEY_URL` at the gateway's Valkey (`<gateway>:6379`) and `CALIBAN_QDRANT_URL` at its Qdrant REST port (6333). The compose config sets `[limits] store = "valkey"`, so quotas and `Idempotency-Key` records are shared by the gateway and every router. Routers can be added to a running testbed (`router_count`); the other hosts are not replaced.
2. Check every router: `curl -s http://<router ip>:8080/healthz`. On a router, `sudo docker logs caliban-router` shows the snapshot version it applied.
3. Propagation: create a key or change a route on the gateway, then time how long until every router honours it (one poll interval, about 10 s).
4. Fail-static: on the gateway, `cd $CALIBAN_DEPLOY_DIR/compose && sudo docker compose -f docker-compose.yml -f /opt/caliban-testbed/compose.testbed-gateway.yml stop caliban`; the routers keep serving. `sudo docker restart caliban-router` on a router while the control plane is down: it serves from `/var/lib/caliban/snapshot.json`.
5. Load: run the same `oha` or bench command against each router IP at once and compare with (a). For the shared quota, add `[limits.tenants.<tenant id>]` with `requests_per_minute` to `/opt/caliban-testbed/caliban.toml` on the gateway, recreate `caliban`, and send requests alternating between routers: the limit holds across all of them together. core `bench/scripts/split.py` automates this, `Idempotency-Key` across routers (replay, 409, 422), BYOK keys opened by every router, and fail-static.
6. KEK rotation (core README, "KEK rotation"): routers read `/etc/caliban-testbed/router.env`, which has `CALIBAN_KEK` but no `CALIBAN_KEK_PREVIOUS`. On each router, edit that file (new `CALIBAN_KEK`, old key in `CALIBAN_KEK_PREVIOUS`) and recreate the container with the `docker run` line from `bootstrap/router.sh`; then the gateway's `compose/.env` and `docker compose ... up -d --force-recreate caliban`; then `docker compose exec caliban caliban keys rotate` and `keys status`. Keep the new key on the hosts (do not copy it through the bucket or SSM by hand). The snapshot version does not change on `keys rotate`, so check the routers by sending a BYOK request through each before removing `CALIBAN_KEK_PREVIOUS`.

### (c) GPU open-model tier

```bash
tofu apply -var gpu_enabled=true              # the semantic cache and intent kNN are on core main
```

1. The GPU host (spot; `-var gpu_market=on-demand` if spot capacity is short) fetches Qwen3.8-27B-FP8, Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B with `airgap/bundle.sh fetch` (unpinned revisions are allowed by `hf_allow_unpinned` until `models.lock.yaml` is pinned) and starts the compose profiles `qwen3-large embeddings reranker` with the `.env.example` defaults that fit 48 GB (vLLM 0.30 needs `QWEN3_LARGE_MAX_NUM_SEQS=32` and `QWEN3_LARGE_GPU_UTIL=0.82` there; see the comment in `docker-compose.yml`). Loading the 29 GB of weights from the gp3 volume takes about 4 minutes per vLLM start. Watch it: `cd $CALIBAN_DEPLOY_DIR/compose && sudo docker compose ps` and `nvidia-smi`.
2. The gateway's providers point at the GPU host (`gateway_use_gpu_models`). Check from the loadgen:

   ```bash
   curl -s "http://$GPU_IP:8000/v1/models"; curl -s "http://$GPU_IP:8001/health"
   curl -s "http://$GPU_IP:8001/v1/embeddings" -H 'Content-Type: application/json' \
     -d '{"model":"Qwen/Qwen3-Embedding-0.6B","input":"hello"}' | jq '.data[0].embedding | length'   # 1024
   ```

3. Semantic cache and intent classifier against the real embedder. `[cache]` and `[routing]` always come from the file (Postgres only holds tenants, models and routes). `/opt/caliban-testbed/caliban.toml` is a copy of `compose/config/caliban.toml`, which already carries the calibrated values for Qwen3-Embedding-0.6B: a `[cache.semantic]` table (`store = "qdrant"`, `min_threshold = 0.93`) with `enabled = false`, and a commented-out `[routing]` table (`query_prefix`, `temperature = 0.1`, `abstain_threshold = 0.6`, `oos_threshold = 0.64`; the `\n` in `query_prefix` is a TOML escape for the newline the Qwen3 instruction format needs). On the gateway, turn both on and recreate the service:

   ```bash
   TOML=/opt/caliban-testbed/caliban.toml
   sudo sed -i '/^\[cache.semantic\]/,/^\[/ s/^enabled = false/enabled = true/' "$TOML"
   sudo sed -i '/^# \[routing\]/,/^# oos_threshold/ s/^# //' "$TOML"
   cd $CALIBAN_DEPLOY_DIR/compose && sudo docker compose -f docker-compose.yml -f /opt/caliban-testbed/compose.testbed-gateway.yml up -d --force-recreate --wait caliban
   ```

   Then create a tenant with `"semantic_cache": "on"` (`POST /api/v1/tenants`), give it a key and a route, and send paraphrased and near-miss prompts with `temperature` at most 0.3 through `http://$GATEWAY_IP:8080/v1/chat/completions` (`x-caliban-cache-tier: semantic` marks a hit). For the intent classifier on its own, run core's `knn_eval` test on the loadgen with `CALIBAN_KNN_EVAL_URL=http://$GPU_IP:8001/v1 CALIBAN_KNN_EVAL_MODEL=Qwen/Qwen3-Embedding-0.6B`.
4. vLLM throughput, inside the vLLM container on the GPU host (random dataset, no download):

   ```bash
   cd $CALIBAN_DEPLOY_DIR/compose
   sudo docker compose exec qwen3-large vllm bench serve --backend openai-chat \
     --base-url http://127.0.0.1:8000 --endpoint /v1/chat/completions \
     --model Qwen/Qwen3.8-27B-FP8 --tokenizer /models/qwen3.8-27b-fp8 \
     --dataset-name random --random-input-len 1024 --random-output-len 256 \
     --num-prompts 200 --max-concurrency 16
   ```

   Then the same load through Caliban from the loadgen (`MODEL=local/qwen3.8-27b`) to see the gateway's share. To run everything on the GPU host instead (the README's single-host tier), set `gpu_run_caliban = true` (the x86_64 source build on 4 vCPUs is slow; use an amd64 bundle with `image_source = "s3"`).

### (d) Zero-egress install from an offline bundle

```bash
# bundles and tools uploaded first (see "Images, bundles and tools in the bucket")
tofu apply -var network_mode=isolated -var enable_flow_logs=true \
  -var 'bundle_pubkey=<contents of caliban-bundle.pub>'
# with the GPU: add -var gpu_enabled=true (amd64 bundle with the weights)
```

1. Every host is in the isolated subnet, with no public IP, and loads the Caliban image (and on the GPU host, vLLM, TEI and the weights) from the bucket with `load.sh`, which checks the signature and every file. Docker comes from the Amazon Linux 2023 repositories through the S3 endpoint; compose from `tools/<arch>/docker-compose`. The three SSM endpoints come up automatically.
2. Check the install: on the gateway, `cd $CALIBAN_DEPLOY_DIR/compose && sudo ../scripts/smoke.sh` (health and admin API). For a chat completion without the GPU, run `mock-upstream` (core bench) on the connected loadgen on a port the security group admits (8000-8003 or 9000-9001), register it as a provider and model through the admin API, route the default tenant to it, and run `sudo MINT_KEY=1 MODEL=<that model> ../scripts/smoke.sh`.
3. Egress attempts must fail, from the host and from the containers:

   ```bash
   curl -sS -m 5 https://example.com && echo "EGRESS WORKS: FAIL" || echo "blocked: ok"
   curl -sS -m 5 http://1.1.1.1 && echo "EGRESS WORKS: FAIL" || echo "blocked: ok"
   curl -sS -m 5 https://s3.eu-west-1.amazonaws.com && echo "FAIL" || echo "other region blocked: ok"
   aws s3 ls s3://some-other-bucket 2>&1 | grep -q AccessDenied && echo "other bucket denied: ok"
   sudo nsenter -t "$(sudo docker inspect -f '{{.State.Pid}}' caliban-caliban-1)" -n \
     curl -sS -m 5 https://example.com || echo "caliban container blocked: ok"
   aws s3 ls "s3://$CALIBAN_TESTBED_BUCKET/bundles/" && echo "testbed bucket reachable: expected"
   ```

   The `curl` calls time out: there is no route, and the security group has no rule for them. DNS still answers (see the reachability table).
4. Flow logs (`tofu output network` gives the log group and the isolated subnet id). In CloudWatch Logs Insights on `/caliban-testbed/vpc-flow-logs`, after the tests above:

   ```
   parse @message "* * * * * * * * * * * * * * * * * * *" as version, eni, subnet, instance, src, dst, srcport, dstport, proto, packets, bytes, start, end, action, status, direction, path, pktsrc, pktdst
   | filter subnet = "<isolated subnet id>" and direction = "egress"
   | filter not isIpv4InSubnet(dst, "10.42.0.0/16")
   | stats count(*) as flows, sum(bytes) as total_bytes by action, path, dst
   | sort flows desc
   ```

   Expected: `ACCEPT` rows only, all with `path = 7` (S3 gateway endpoint). No row may have `path = 8` (internet gateway); adding `| filter path != "7"` must return nothing. The blocked `curl` attempts do not appear at all (the security group denies them before they become flows), so the evidence is that absence plus the failed attempts in step 3. Traffic to the SSM endpoints stays inside the VPC CIDR and is filtered out by the second line. (Field aliases must not reuse a parsed name: `sum(bytes) as bytes` is rejected.)
5. Turn flow logs and the GPU off again when done.

### (e) Single sign-on with Dex on the gateway

core `bench/scripts/sso_dex_testbed.sh`, run as root on the gateway, starts Dex (`ghcr.io/dexidp/dex:v2.43.1`, in-memory) on the gateway's private IP, port 8003 (admitted by the security group), with a `mock` connector (user "Kilgore Trout", group `authors`, mapped to owner) and a password user `viewer@example.com` (no groups), then sets the `CALIBAN_OIDC_*` variables in `compose/.env` and recreates `caliban`. Plain http: the control plane warns that tokens and cookies are not secure, which is expected here. From the loadgen, core `bench/scripts/sso_e2e.py` runs the login flow, CSRF and Origin checks, a viewer bound through the role-binding API, logout and the audit log; `bench/scripts/ui_login.py` signs in through the console in headless Chromium (the official Playwright container). The admin token keeps working as the break-glass credential, and every use of it is audited.

## Teardown checklist

1. Copy what you need: `aws s3 sync s3://<bucket>/results ./results $P`.
2. Destroy: `cd deploy/aws/testbed && tofu destroy`. With bundles still in the bucket, either delete them first (`aws s3 rm s3://<bucket> --recursive $P`, then remove old versions in the console) or set `bucket_force_destroy = true`, `tofu apply`, then `tofu destroy`.
3. Check nothing is left, using the ids from `tofu output` before destroying:
   - `aws ec2 describe-instances $P --instance-ids <ids> --query 'Reservations[].Instances[].State.Name'` shows `terminated`;
   - `aws ec2 describe-spot-instance-requests $P --spot-instance-request-ids <ids>` shows no `open` or `active` request; cancel any that remain with `aws ec2 cancel-spot-instance-requests` (the spot request ids are in the EC2 console or `aws ec2 describe-instances` before the destroy);
   - `aws ec2 describe-volumes $P --filters Name=attachment.instance-id,Values=<ids>` is empty, and no unattached volumes from the testbed remain;
   - `aws ec2 describe-addresses $P` shows no Elastic IP from the NAT gateway;
   - `aws ec2 describe-vpc-endpoints $P --filters Name=vpc-id,Values=<vpc id>` is empty;
   - `aws scheduler list-schedules $P --name-prefix caliban-testbed` is empty;
   - `aws ssm get-parameters-by-path $P --path /caliban-testbed` is empty.
4. Remove local state if you will not use it again: `terraform.tfstate*` hold the old secrets.

**Parking instead of destroying.** `tofu apply -var gateway_enabled=false -var loadgen_enabled=false -var router_count=0 -var gpu_enabled=false -var enable_flow_logs=false` keeps the VPC, bucket and parameters (pennies) and removes every hourly cost. Interface endpoints switch off with the hosts.

## Validation (offline)

None of these call AWS. `tofu test` plans every scenario against a mocked AWS provider.

```bash
cd deploy/aws/testbed
tofu fmt -check -recursive
tofu init -backend=false
tofu validate
tofu test
docker run --rm -v "$PWD":/data -w /data --entrypoint /bin/sh ghcr.io/terraform-linters/tflint \
  -c 'tflint --init && tflint'
docker run --rm -v "$PWD":/src aquasec/trivy config /src
docker run --rm -v "$PWD":/tf bridgecrew/checkov -d /tf --framework terraform --compact
docker run --rm -v "$PWD/bootstrap":/mnt -w /mnt koalaman/shellcheck:v0.11.0 -x *.sh
(cd ../../compose && cp .env.example .env && \
  CALIBAN_ADMIN_TOKEN=x CALIBAN_KEK=x POSTGRES_PASSWORD=x VALKEY_PASSWORD=x \
  CALIBAN_SNAPSHOT_SIGNING_KEY=x CALIBAN_ROUTER_TOKEN=x TESTBED_BIND=10.42.0.10 \
  docker compose -f docker-compose.yml -f ../aws/testbed/bootstrap/compose.testbed-gateway.yml \
    -f ../aws/testbed/bootstrap/compose.testbed-models.yml \
    --profile qwen3-large --profile embeddings --profile reranker config -q)
```

Accepted scanner findings are annotated in place with `trivy:ignore` and `checkov:skip` comments and a reason: SSE-S3 rather than KMS (as specified), no bucket access logging or replication, flow logs as a toggle, outbound HTTPS from connected hosts only, no detailed monitoring.

## Known limits

- The first `tofu plan` reads AWS (AZ offerings, AMI parameters, account id). There is no other way to resolve them; `tofu test` covers the logic offline.
- The DLAMI public parameter path and the Docker plugin versions (`compose_version`, `buildx_version`) are defaults to check against the current releases.
- Spot capacity for g6e in Frankfurt can be short, and it is per AZ: the module picks the first AZ that offers the types, not one with spot capacity. The provider keeps retrying `InsufficientInstanceCapacity` until its create timeout. The error message (CloudTrail `RunInstances`) names the AZs that have capacity; set `availability_zone` to one of them (it moves every host), or switch `gpu_market` to `on-demand`.
- An instance can come up without its SSM agent registered (seen once on the first apply, when the instance profile was not yet visible to EC2). If a host does not appear in `aws ssm describe-instance-information` a few minutes after boot, reboot it.
- `tofu test` reads `terraform.tfvars` from the module directory, so local values (for example `gpu_enabled = true`) make some offline tests fail. Run the tests from a copy without it, or move it aside.
- **vLLM start order on the 48 GB tier.** In the second run the 27B model profiled its memory while the reranker was still starting, got 1.2 GiB of KV cache (one 131072-token sequence needs 4.3 GiB) and crash-looped. Restarting it alone (`docker compose ... up -d --force-recreate qwen3-large`) once the embedder and reranker are healthy, with `QWEN3_LARGE_MAX_MODEL_LEN=32768` in `compose/.env`, gave 5.15 GiB. Check `docker compose ps` on the GPU host before measuring.
- **Background processes and SSM Run Command.** A process started with `nohup ... &` inside an `aws ssm send-command` script is killed when the command ends or times out. Start long-running helpers (mock upstreams, load generators) with `setsid nohup ... &`.
- **Ports between hosts.** The hosts security group admits 8080, 8081, 5432, 6379, 6333-6334, 8000-8003 and 9000-9001 inside the VPC; put extra services (a mock upstream, Dex for single sign-on) on one of those ports.
- DNS resolution is not blocked in the isolated subnet (see the reachability table).
