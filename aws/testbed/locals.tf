data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  # checkov:skip=CKV_AWS_394: only a fallback; one AZ is chosen and then kept in state
  state = "available"
}

# AZs that offer the GPU type and the CPU type (g6e is not in every AZ).
data "aws_ec2_instance_type_offerings" "gpu" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.gpu_instance_type]
  }
}

data "aws_ec2_instance_type_offerings" "cpu" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [local.cpu_types.gateway]
  }
}

locals {
  prefix = var.name_prefix

  # ── architecture and instance types ──
  cpu_defaults = {
    arm64  = { large = "c8g.2xlarge", small = "c8g.xlarge", ami = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64" }
    x86_64 = { large = "c7i.2xlarge", small = "c7i.xlarge", ami = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" }
  }
  cpu_types = {
    gateway = coalesce(var.gateway_instance_type, local.cpu_defaults[var.cpu_arch].large)
    router  = coalesce(var.router_instance_type, local.cpu_defaults[var.cpu_arch].small)
    loadgen = coalesce(var.loadgen_instance_type, local.cpu_defaults[var.cpu_arch].large)
  }
  cpu_ami_parameter = coalesce(var.cpu_ami_ssm_parameter, local.cpu_defaults[var.cpu_arch].ami)
  # Docker platform names, used for bundle and tools paths.
  docker_arch = { cpu = var.cpu_arch == "arm64" ? "arm64" : "amd64", gpu = "amd64" }

  # ── AZ ──
  az_candidates = sort(setintersection(
    toset(data.aws_ec2_instance_type_offerings.gpu.locations),
    toset(data.aws_ec2_instance_type_offerings.cpu.locations),
  ))
  az = coalesce(var.availability_zone, try(local.az_candidates[0], data.aws_availability_zones.available.names[0]))

  # ── subnets: the first three /24s of the VPC ──
  subnet_cidrs = {
    public   = cidrsubnet(var.vpc_cidr, 8, 0)
    private  = cidrsubnet(var.vpc_cidr, 8, 1)
    isolated = cidrsubnet(var.vpc_cidr, 8, 2)
  }

  role_tier = { for r in ["gateway", "router", "loadgen", "gpu"] : r => lookup(var.role_network_mode, r, var.network_mode) }

  # A tier is "connected" when it has a route to the internet.
  tier_connected = {
    public   = true
    private  = var.enable_nat_gateway
    isolated = false
  }

  # Fixed private IPs so hosts can find each other without depending on creation order.
  host_index = { gateway = 10, gpu = 20, loadgen = 30, router = 40 }

  # ── hosts ──
  hosts_list = concat(
    var.gateway_enabled ? [{ key = "gateway", role = "gateway", n = 0 }] : [],
    [for i in range(var.router_count) : { key = "router-${i + 1}", role = "router", n = i }],
    var.loadgen_enabled ? [{ key = "loadgen", role = "loadgen", n = 0 }] : [],
    var.gpu_enabled ? [{ key = "gpu", role = "gpu", n = 0 }] : [],
  )

  hosts = {
    for h in local.hosts_list : h.key => {
      role         = h.role
      tier         = local.role_tier[h.role]
      connected    = local.tier_connected[local.role_tier[h.role]]
      ip           = cidrhost(local.subnet_cidrs[local.role_tier[h.role]], local.host_index[h.role] + h.n)
      gpu          = h.role == "gpu"
      type         = h.role == "gpu" ? var.gpu_instance_type : local.cpu_types[h.role]
      spot         = h.role == "gpu" ? var.gpu_market == "spot" : var.cpu_use_spot
      root_gb      = h.role == "gpu" ? (local.role_tier.gpu == "isolated" ? max(var.gpu_root_volume_gb, var.gpu_isolated_root_volume_gb) : var.gpu_root_volume_gb) : var.cpu_root_volume_gb
      docker_arch  = h.role == "gpu" ? local.docker_arch.gpu : local.docker_arch.cpu
      image_source = local.role_tier[h.role] == "isolated" ? "s3" : var.image_source
    }
  }

  ip_of = {
    gateway = var.gateway_enabled ? cidrhost(local.subnet_cidrs[local.role_tier.gateway], local.host_index.gateway) : ""
    gpu     = var.gpu_enabled ? cidrhost(local.subnet_cidrs[local.role_tier.gpu], local.host_index.gpu) : ""
    loadgen = var.loadgen_enabled ? cidrhost(local.subnet_cidrs[local.role_tier.loadgen], local.host_index.loadgen) : ""
    routers = [for k, h in local.hosts : h.ip if h.role == "router"]
  }

  any_offline_host = anytrue([for h in values(local.hosts) : !h.connected])
  ssm_endpoints    = var.enable_ssm_endpoints == null ? local.any_offline_host : var.enable_ssm_endpoints

  # ── vCPU accounting for the quota check (size suffix -> vCPUs) ──
  size_vcpus = {
    "medium"   = 1, "large" = 2, "xlarge" = 4, "2xlarge" = 8, "4xlarge" = 16, "8xlarge" = 32,
    "12xlarge" = 48, "16xlarge" = 64, "24xlarge" = 96, "48xlarge" = 192,
  }
  vcpus_of        = { for k, h in local.hosts : k => lookup(local.size_vcpus, split(".", h.type)[1], 0) }
  standard_vcpus  = sum(concat([0], [for k, h in local.hosts : local.vcpus_of[k] if !h.gpu]))
  gpu_family_vcpu = sum(concat([0], [for k, h in local.hosts : local.vcpus_of[k] if h.gpu]))

  param_prefix = "/${local.prefix}"
}
