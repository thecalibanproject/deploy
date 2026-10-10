# Every host: IMDSv2 required (hop limit 1, so containers cannot reach IMDS), encrypted gp3
# root volume, the SSM-only instance profile, no key pair, no inbound from outside the VPC.

data "aws_ssm_parameter" "cpu_ami" {
  count = length([for h in values(local.hosts) : h if !h.gpu]) > 0 ? 1 : 0
  name  = local.cpu_ami_parameter
}

data "aws_ssm_parameter" "gpu_ami" {
  count = var.gpu_enabled ? 1 : 0
  name  = var.gpu_ami_ssm_parameter
}

locals {
  bootstrap_dir = "${path.module}/bootstrap"

  # Non-secret settings for the bootstrap scripts (/etc/caliban-testbed/testbed.env).
  host_env = {
    for k, h in local.hosts : k => {
      HOST_KEY               = k
      ROLE                   = h.role
      TIER                   = h.tier
      CONNECTED              = tostring(h.connected)
      SELF_IP                = h.ip
      REGION                 = var.region
      BUCKET                 = aws_s3_bucket.this.id
      PARAM_PREFIX           = local.param_prefix
      DOCKER_ARCH            = h.docker_arch
      IMAGE_SOURCE           = h.image_source
      CALIBAN_VERSION        = var.caliban_version
      GITHUB_ORG             = var.github_org
      CORE_REF               = var.core_ref
      WEB_REF                = var.web_ref
      DEPLOY_REF             = var.deploy_ref
      BUNDLE_KEY             = "${var.bundle_s3_prefix}/${h.docker_arch}/caliban-bundle-${var.caliban_version}.tar"
      BUNDLE_VERIFY          = var.bundle_verify
      TOOLS_PREFIX           = "${var.tools_s3_prefix}/${h.docker_arch}"
      COMPOSE_VERSION        = var.compose_version
      BUILDX_VERSION         = var.buildx_version
      GATEWAY_IP             = local.ip_of.gateway
      GPU_IP                 = local.ip_of.gpu
      LOADGEN_IP             = local.ip_of.loadgen
      GATEWAY_USE_GPU_MODELS = tostring(var.gateway_use_gpu_models && var.gpu_enabled)
      GPU_PROFILES           = join(" ", var.gpu_compose_profiles)
      GPU_RUN_CALIBAN        = tostring(var.gpu_run_caliban)
      GPU_FETCH_ARGS         = var.gpu_weights_fetch_args
      HF_ALLOW_UNPINNED      = tostring(var.hf_allow_unpinned)
      BENCH_REF              = var.bench_ref
      BENCH_PACKAGE          = var.bench_package
      CARGO_TOOLS            = join(" ", var.loadgen_cargo_tools)
      TTL_HOURS              = tostring(var.ttl_hours)
      # The OS power-off timer is for on-demand hosts; spot hosts rely on the scheduler.
      OS_TTL_TIMER = tostring(!h.spot)
    }
  }
}

locals {
  # cloud-config part: settings, the shared helpers, the testbed's own copy of
  # airgap/load.sh and the compose overrides. The role script is the second part.
  cloud_config = {
    for k, h in local.hosts : k => join("", ["#cloud-config\n", yamlencode(
      {
        write_files = concat(
          [
            {
              path        = "/etc/caliban-testbed/testbed.env"
              permissions = "0644"
              content     = join("", [for key, val in local.host_env[k] : "${key}='${val}'\n"])
            },
            { path = "/opt/caliban-testbed/common.sh", permissions = "0755", content = file("${local.bootstrap_dir}/common.sh") },
            { path = "/opt/caliban-testbed/load.sh", permissions = "0755", content = file("${path.module}/../../airgap/load.sh") },
            { path = "/opt/caliban-testbed/compose.testbed-gateway.yml", permissions = "0644", content = file("${local.bootstrap_dir}/compose.testbed-gateway.yml") },
            { path = "/opt/caliban-testbed/compose.testbed-models.yml", permissions = "0644", content = file("${local.bootstrap_dir}/compose.testbed-models.yml") },
          ],
          # Only on hosts that load a bundle, so setting the key does not replace the others.
          var.bundle_pubkey == null || h.image_source != "s3" ? [] : [
            { path = "/etc/caliban-testbed/bundle.pub", permissions = "0644", content = var.bundle_pubkey },
          ],
        )
    })])
  }
}

data "cloudinit_config" "host" {
  for_each = local.hosts

  gzip          = true
  base64_encode = true

  part {
    content_type = "text/cloud-config"
    filename     = "testbed-files.yaml"
    content      = local.cloud_config[each.key]
  }

  part {
    content_type = "text/x-shellscript"
    filename     = "${each.value.role}.sh"
    content      = file("${local.bootstrap_dir}/${each.value.role}.sh")
  }
}

resource "aws_instance" "host" {
  # checkov:skip=CKV_AWS_126: detailed monitoring is billed per instance; 5-minute metrics are enough here
  for_each = local.hosts

  ami                         = each.value.gpu ? data.aws_ssm_parameter.gpu_ami[0].insecure_value : data.aws_ssm_parameter.cpu_ami[0].insecure_value
  instance_type               = each.value.type
  availability_zone           = local.az
  subnet_id                   = local.subnet_ids[each.value.tier]
  private_ip                  = each.value.ip
  associate_public_ip_address = each.value.tier == "public"
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  ebs_optimized               = true
  monitoring                  = false # detailed monitoring is billed; not needed for a testbed

  vpc_security_group_ids = concat(
    [aws_security_group.hosts.id],
    each.value.connected ? [aws_security_group.internet_egress.id] : [],
  )

  user_data_base64            = data.cloudinit_config.host[each.key].rendered
  user_data_replace_on_change = true

  # On-demand hosts stop (not terminate) when the OS powers off (the TTL timer).
  instance_initiated_shutdown_behavior = each.value.spot ? null : "stop"

  dynamic "instance_market_options" {
    for_each = each.value.spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        spot_instance_type             = "persistent"
        instance_interruption_behavior = "stop"
        max_price                      = each.value.gpu ? var.gpu_spot_max_price : null
      }
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = each.value.root_gb
    encrypted             = true
    delete_on_termination = true
  }

  lifecycle {
    # A new AMI release must not replace a host in the middle of a run.
    ignore_changes = [ami]

    precondition {
      condition     = !(each.value.tier == "private" && !var.enable_nat_gateway && each.value.image_source == "build")
      error_message = "${each.key}: image_source = \"build\" needs internet access. Use the public tier, enable_nat_gateway = true, or image_source = \"s3\"."
    }
    precondition {
      condition     = each.value.role != "router" || var.gateway_enabled
      error_message = "${each.key}: routers poll the gateway's control plane and use its Valkey; set gateway_enabled = true."
    }
    precondition {
      condition     = each.value.image_source != "s3" || var.bundle_verify == "none" || var.bundle_pubkey != null
      error_message = "${each.key}: loading a signed bundle needs bundle_pubkey (or bundle_verify = \"none\")."
    }
    precondition {
      condition     = !(each.value.gpu && !each.value.connected && each.value.image_source != "s3")
      error_message = "The GPU host without internet access must load weights from an S3 bundle."
    }
  }

  depends_on = [
    aws_ssm_parameter.secret,
    aws_ssm_parameter.snapshot_public_pem,
    aws_vpc_endpoint.ssm,
    aws_route.public_internet,
    aws_route.private_nat,
    aws_iam_role_policy.bucket_access,
    aws_iam_role_policy_attachment.ssm_core,
  ]
}
