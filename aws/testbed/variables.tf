# ─────────────────────────── Account and region ───────────────────────────

variable "region" {
  description = "AWS region. Prices in README.md are for eu-central-1 (Frankfurt)."
  type        = string
  default     = "eu-central-1"
}

variable "aws_profile" {
  description = "AWS CLI profile used by the provider."
  type        = string
  default     = "default"
}

variable "allowed_account_ids" {
  description = "Optional guard: the provider refuses to run against any other account. Set in terraform.tfvars (git-ignored)."
  type        = list(string)
  default     = null
}

variable "name_prefix" {
  description = "Prefix for every named resource (bucket, security groups, IAM, SSM parameters, log group, schedule)."
  type        = string
  default     = "caliban-testbed"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name_prefix))
    error_message = "name_prefix must be 3 to 25 characters of lowercase letters, digits and hyphens."
  }
}

variable "availability_zone" {
  description = "AZ for every subnet and host. null = the first AZ (sorted) that offers both the GPU and the CPU instance types."
  type        = string
  default     = null
}

# ─────────────────────────── Network ───────────────────────────

variable "vpc_cidr" {
  description = "VPC CIDR. Subnets are the first three /24s: public, private, isolated."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) <= 22
    error_message = "vpc_cidr must be a valid IPv4 CIDR of /22 or larger."
  }
}

variable "network_mode" {
  description = <<-EOT
    Subnet tier for every host unless role_network_mode overrides it:
      public    public subnet, public IPv4, no inbound rules; egress 80/443 to the internet (builds, Hugging Face).
      private   private subnet; internet only through the optional NAT gateway.
      isolated  isolated subnet: no IGW, no NAT. Only the SSM endpoints and the S3 gateway endpoint. Forces image_source = "s3".
  EOT
  type        = string
  default     = "public"

  validation {
    condition     = contains(["public", "private", "isolated"], var.network_mode)
    error_message = "network_mode must be public, private or isolated."
  }
}

variable "role_network_mode" {
  description = "Per-role override of network_mode, e.g. { loadgen = \"public\" }. Roles: gateway, router, loadgen, gpu."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k, v in var.role_network_mode :
      contains(["gateway", "router", "loadgen", "gpu"], k) && contains(["public", "private", "isolated"], v)
    ])
    error_message = "Keys must be gateway, router, loadgen or gpu; values public, private or isolated."
  }
}

variable "enable_nat_gateway" {
  description = "NAT gateway for the private subnet (about $0.05/h plus $0.05/GB). Off by default: use the public tier or VPC endpoints."
  type        = bool
  default     = false
}

variable "enable_ssm_endpoints" {
  description = "Interface endpoints ssm, ssmmessages, ec2messages (about $0.012/h each). null = on when a host runs in a tier without internet access."
  type        = bool
  default     = null
}

variable "s3_endpoint_allow_al2023_repos" {
  description = "Let the S3 gateway endpoint reach the Amazon Linux 2023 package repository bucket, so hosts without internet can `dnf install docker`."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "VPC Flow Logs to CloudWatch Logs (zero-egress evidence for scenario d). Billed per GB ingested."
  type        = bool
  default     = false
}

variable "flow_logs_retention_days" {
  description = "Retention of the flow log group."
  type        = number
  default     = 7
}

# ─────────────────────────── Hosts ───────────────────────────

variable "cpu_arch" {
  description = "Architecture of the CPU hosts (gateway, routers, loadgen). The Caliban image builds for both; arm64 = Graviton."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.cpu_arch)
    error_message = "cpu_arch must be arm64 or x86_64."
  }
}

variable "cpu_ami_ssm_parameter" {
  description = "Public SSM parameter for the CPU host AMI. null = Amazon Linux 2023 for cpu_arch."
  type        = string
  default     = null
}

variable "cpu_root_volume_gb" {
  description = "Root gp3 volume of the CPU hosts (a source build needs about 25 GB of Docker cache)."
  type        = number
  default     = 60
}

variable "cpu_use_spot" {
  description = "Run the CPU hosts on spot (persistent, stop on interruption). Off by default so benchmark runs are not interrupted."
  type        = bool
  default     = false
}

variable "gateway_enabled" {
  description = "Gateway host: Caliban standalone with Postgres, Valkey and Qdrant (compose)."
  type        = bool
  default     = true
}

variable "gateway_instance_type" {
  description = "null = c8g.2xlarge (arm64) or c7i.2xlarge (x86_64): 8 vCPU, 16 GiB."
  type        = string
  default     = null
}

variable "gateway_use_gpu_models" {
  description = "When the GPU host is enabled, point the gateway's model providers at the GPU host's published ports instead of local compose service names."
  type        = bool
  default     = true
}

variable "router_count" {
  description = "Extra hosts running `caliban router` in split/snapshot mode against the gateway."
  type        = number
  default     = 0

  validation {
    condition     = var.router_count >= 0 && var.router_count <= 8 && floor(var.router_count) == var.router_count
    error_message = "router_count must be an integer from 0 to 8."
  }
}

variable "router_instance_type" {
  description = "null = c8g.xlarge (arm64) or c7i.xlarge (x86_64): 4 vCPU, 8 GiB."
  type        = string
  default     = null
}

variable "loadgen_enabled" {
  description = "Load generator host in the same AZ."
  type        = bool
  default     = true
}

variable "loadgen_instance_type" {
  description = "null = c8g.2xlarge (arm64) or c7i.2xlarge (x86_64)."
  type        = string
  default     = null
}

variable "gpu_enabled" {
  description = "GPU host (1 x L40S 48 GB) running the compose model profiles. Off by default: it is the expensive part."
  type        = bool
  default     = false
}

variable "gpu_instance_type" {
  description = "GPU instance type. g6e.xlarge = 1 x L40S 48 GB, 4 vCPU, 32 GiB."
  type        = string
  default     = "g6e.xlarge"
}

variable "gpu_market" {
  description = "spot (persistent request, stops on interruption) or on-demand (fallback when spot capacity is short)."
  type        = string
  default     = "spot"

  validation {
    condition     = contains(["spot", "on-demand"], var.gpu_market)
    error_message = "gpu_market must be spot or on-demand."
  }
}

variable "gpu_spot_max_price" {
  description = "Optional spot price cap in USD/hour. null = capped at the on-demand price."
  type        = string
  default     = null
}

variable "gpu_ami_ssm_parameter" {
  description = "Public SSM parameter of the Deep Learning Base AMI (Ubuntu, NVIDIA driver, Docker, NVIDIA Container Toolkit)."
  type        = string
  default     = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
}

variable "gpu_root_volume_gb" {
  description = "GPU root gp3 volume in connected mode: DLAMI (about 50 GB used) + weights (about 33 GB) + vLLM/TEI images."
  type        = number
  default     = 150
}

variable "gpu_isolated_root_volume_gb" {
  description = "GPU root volume when the GPU host is isolated: the bundle tar and its extraction need room next to the copied weights."
  type        = number
  default     = 300
}

variable "gpu_compose_profiles" {
  description = "Compose profiles started on the GPU host (README hardware tier 1 x 48 GB)."
  type        = list(string)
  default     = ["qwen3-large", "embeddings", "reranker"]
}

variable "gpu_run_caliban" {
  description = "Also run Caliban and the datastores on the GPU host (all-in-one). Needs the Caliban image for x86_64 there (a 4 vCPU source build is slow)."
  type        = bool
  default     = false
}

variable "gpu_weights_fetch_args" {
  description = "Arguments for `airgap/bundle.sh fetch` on a connected GPU host. Qwen3.8-27B-FP8 is bundle: false, so it is selected with --model."
  type        = string
  default     = "--profile embeddings,reranker --model Qwen/Qwen3.8-27B-FP8"
}

variable "hf_allow_unpinned" {
  description = "Pass --allow-unpinned to bundle.sh fetch while models.lock.yaml still has TODO-pin-commit revisions (fetches main)."
  type        = bool
  default     = true
}

# ─────────────────────────── Caliban image and bundles ───────────────────────────

variable "image_source" {
  description = "build = clone core, web and deploy from GitHub and run images/build.sh on the host; s3 = load an airgap/bundle.sh bundle from the bucket. Isolated hosts always use s3."
  type        = string
  default     = "build"

  validation {
    condition     = contains(["build", "s3"], var.image_source)
    error_message = "image_source must be build or s3."
  }
}

variable "github_org" {
  description = "GitHub organisation holding the public core, web and deploy repos."
  type        = string
  default     = "thecalibanproject"
}

variable "core_ref" {
  description = "Branch or tag of core to build (e.g. feat/semantic-cache for scenario c)."
  type        = string
  default     = "main"
}

variable "web_ref" {
  description = "Branch or tag of web to build."
  type        = string
  default     = "main"
}

variable "deploy_ref" {
  description = "Branch or tag of deploy (compose files, build.sh, bundle.sh) used on build-mode hosts."
  type        = string
  default     = "main"
}

variable "caliban_version" {
  description = "Image tag: build.sh --version in build mode; the bundle's version in s3 mode."
  type        = string
  default     = "0.1.0"
}

variable "bundle_s3_prefix" {
  description = "Bundles live at s3://<bucket>/<prefix>/<arch>/caliban-bundle-<version>.tar (+ .tar.sha256), arch = arm64 or amd64."
  type        = string
  default     = "bundles"
}

variable "bundle_verify" {
  description = "Signature check for bundles: minisign, cosign or none (load.sh --allow-unsigned)."
  type        = string
  default     = "minisign"

  validation {
    condition     = contains(["minisign", "cosign", "none"], var.bundle_verify)
    error_message = "bundle_verify must be minisign, cosign or none."
  }
}

variable "bundle_pubkey" {
  description = "Public key that verifies the bundle signature (contents of the minisign .pub or cosign.pub). Passed out of band, like on a real site; keep it in terraform.tfvars."
  type        = string
  default     = null
}

variable "tools_s3_prefix" {
  description = "Static binaries for hosts without internet: s3://<bucket>/<prefix>/<arch>/{docker-compose,minisign,cosign,caliban-bench}."
  type        = string
  default     = "tools"
}

variable "compose_version" {
  description = "Docker Compose plugin release installed from GitHub when the AMI has none (connected hosts)."
  type        = string
  default     = "v5.1.4"
}

variable "buildx_version" {
  description = "Docker Buildx plugin release installed from GitHub on build-mode hosts (images/build.sh uses buildx)."
  type        = string
  default     = "v0.34.1"
}

# ─────────────────────────── Load generator ───────────────────────────

variable "bench_ref" {
  description = "core branch that holds the bench crate."
  type        = string
  default     = "feat/bench"
}

variable "bench_package" {
  description = "Cargo package name of the bench load generator. Built with `cargo build --release -p` when the branch has it."
  type        = string
  default     = "caliban-bench"
}

variable "loadgen_cargo_tools" {
  description = "Extra crates installed with `cargo install --locked` on a connected loadgen (oha = HTTP load generator)."
  type        = list(string)
  default     = ["oha"]
}

# ─────────────────────────── Bucket, results, safety ───────────────────────────

variable "bucket_force_destroy" {
  description = "Let `tofu destroy` delete the bucket with its bundles and results (all versions)."
  type        = bool
  default     = false
}

variable "results_upload" {
  description = "Also grant s3:PutObject on <bucket>/results/* so hosts can upload bench results. Off = read-only bucket access."
  type        = bool
  default     = false
}

variable "ttl_hours" {
  description = "Auto-stop: an EventBridge Scheduler one-shot stops every host this many hours after the hosts were created (and on-demand hosts power off this long after each boot)."
  type        = number
  default     = 4

  validation {
    condition     = var.ttl_hours >= 1 && var.ttl_hours <= 72
    error_message = "ttl_hours must be between 1 and 72."
  }
}

variable "standard_vcpu_quota" {
  description = "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) vCPU quota in the region; checked before apply."
  type        = number
  default     = 32
}

variable "gpu_vcpu_quota" {
  description = "Running On-Demand (or spot) G and VT vCPU quota in the region; checked before apply."
  type        = number
  default     = 8
}
