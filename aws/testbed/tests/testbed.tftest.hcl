# Offline tests: `tofu test` plans every scenario against a MOCKED AWS provider.
# No credentials are read and no AWS API is called.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-central-1a", "eu-central-1b", "eu-central-1c"] }
  }
  mock_data "aws_ec2_instance_type_offerings" {
    defaults = { locations = ["eu-central-1b", "eu-central-1c"] }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "000000000000" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { insecure_value = "ami-00000000000000000" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::caliban-testbed-mock", id = "caliban-testbed-mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-central-1:000000000000:log-group:mock" }
  }
  mock_resource "aws_instance" {
    defaults = { arn = "arn:aws:ec2:eu-central-1:000000000000:instance/i-mock" }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = { prefix_list_id = "pl-00000000" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

run "defaults_gateway_and_loadgen" {
  command = plan

  assert {
    condition     = toset(keys(aws_instance.host)) == toset(["gateway", "loadgen"])
    error_message = "defaults should bring up gateway and loadgen only"
  }
  assert {
    condition     = aws_instance.host["gateway"].instance_type == "c8g.2xlarge" && aws_instance.host["gateway"].private_ip == "10.42.0.10"
    error_message = "gateway type or IP"
  }
  assert {
    condition     = aws_instance.host["gateway"].metadata_options[0].http_tokens == "required" && aws_instance.host["gateway"].root_block_device[0].encrypted
    error_message = "IMDSv2 and EBS encryption are required"
  }
  assert {
    condition     = local.az == "eu-central-1b"
    error_message = "AZ should be the first one offering both instance types"
  }
  assert {
    condition     = length(aws_vpc_endpoint.ssm) == 0 && length(aws_nat_gateway.this) == 0 && length(aws_flow_log.vpc) == 0
    error_message = "public mode should not create paid endpoints, NAT or flow logs"
  }
  assert {
    condition     = length(aws_scheduler_schedule.ttl_stop) == 1
    error_message = "the TTL schedule must exist whenever hosts exist"
  }
}

run "full_split_and_gpu" {
  command = plan

  variables {
    router_count = 4
    gpu_enabled  = true
  }

  assert {
    condition     = length(aws_instance.host) == 7 && local.standard_vcpus == 32 && local.gpu_family_vcpu == 4
    error_message = "gateway + loadgen + 4 routers + gpu should fit 32 standard and 8 G vCPUs"
  }
  assert {
    condition     = aws_instance.host["gpu"].instance_market_options[0].market_type == "spot" && aws_instance.host["gpu"].root_block_device[0].volume_size == 150
    error_message = "GPU host should be spot with a 150 GB root volume"
  }
  assert {
    condition     = aws_instance.host["router-4"].private_ip == "10.42.0.43" && aws_instance.host["gpu"].private_ip == "10.42.0.20"
    error_message = "fixed host IPs"
  }
  assert {
    # EC2 accepts at most 16 KB of user data before base64 (gzip keeps it well under).
    condition     = alltrue([for k, d in data.cloudinit_config.host : length(d.rendered) < 21800])
    error_message = "user data over 16 KB"
  }
}

run "gpu_on_demand_fallback" {
  command = plan

  variables {
    gpu_enabled = true
    gpu_market  = "on-demand"
  }

  assert {
    condition     = length(aws_instance.host["gpu"].instance_market_options) == 0
    error_message = "on-demand GPU must not request spot"
  }
}

run "isolated_zero_egress" {
  command = plan

  variables {
    network_mode     = "isolated"
    gpu_enabled      = true
    enable_flow_logs = true
    bundle_pubkey    = "untrusted comment: test\nRWTEST"
  }

  assert {
    condition     = toset(keys(aws_vpc_endpoint.ssm)) == toset(["ssm", "ssmmessages", "ec2messages"])
    error_message = "isolated mode needs exactly the three SSM interface endpoints"
  }
  assert {
    condition     = alltrue([for h in values(local.hosts) : h.image_source == "s3" && !h.connected])
    error_message = "isolated hosts must use the S3 bundle"
  }
  assert {
    condition     = aws_instance.host["gpu"].root_block_device[0].volume_size == 300 && aws_instance.host["gateway"].associate_public_ip_address == false
    error_message = "isolated GPU root volume and no public IP"
  }
  assert {
    condition     = length(aws_flow_log.vpc) == 1
    error_message = "flow logs toggle"
  }
}

run "private_with_nat" {
  command = plan

  variables {
    network_mode       = "private"
    enable_nat_gateway = true
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1 && length(aws_vpc_endpoint.ssm) == 0
    error_message = "private + NAT: SSM goes through NAT, no endpoints by default"
  }
}

run "private_without_nat_cannot_build" {
  command = plan

  variables {
    network_mode = "private"
  }

  expect_failures = [aws_instance.host]
}

run "signed_bundle_needs_pubkey" {
  command = plan

  variables {
    image_source = "s3"
  }

  expect_failures = [aws_instance.host]
}

run "x86_hosts" {
  command = plan

  variables {
    cpu_arch     = "x86_64"
    router_count = 1
  }

  assert {
    condition     = aws_instance.host["gateway"].instance_type == "c7i.2xlarge" && aws_instance.host["router-1"].instance_type == "c7i.xlarge"
    error_message = "x86_64 defaults"
  }
}

run "nothing_enabled" {
  command = plan

  variables {
    gateway_enabled = false
    loadgen_enabled = false
  }

  assert {
    condition     = length(aws_instance.host) == 0 && length(aws_scheduler_schedule.ttl_stop) == 0
    error_message = "parked state: no hosts, no schedule"
  }
}
