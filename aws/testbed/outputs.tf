output "account_id" {
  description = "Account the testbed was applied to (not committed anywhere)."
  value       = data.aws_caller_identity.current.account_id
}

output "availability_zone" {
  value = local.az
}

output "hosts" {
  description = "Per host: instance id, role, subnet tier, private IP, type, market."
  value = {
    for k, h in local.hosts : k => {
      id         = aws_instance.host[k].id
      role       = h.role
      tier       = h.tier
      private_ip = h.ip
      type       = h.type
      market     = h.spot ? "spot" : "on-demand"
      image      = h.image_source
    }
  }
}

output "instance_ids" {
  description = "Explicit instance ids (what the auto-stop schedule targets)."
  value       = local.instance_ids
}

output "ssm_sessions" {
  description = "Open a shell on each host (no SSH, no inbound ports)."
  value = {
    for k in keys(local.hosts) : k =>
    "aws ssm start-session --profile ${var.aws_profile} --region ${var.region} --target ${aws_instance.host[k].id}"
  }
}

output "console_port_forward" {
  description = "Forward the gateway's console (8081) and data plane (8080) to localhost."
  value = var.gateway_enabled ? join("\n", [
    for p in ["8081", "8080"] :
    "aws ssm start-session --profile ${var.aws_profile} --region ${var.region} --target ${aws_instance.host["gateway"].id} --document-name AWS-StartPortForwardingSession --parameters '{\"portNumber\":[\"${p}\"],\"localPortNumber\":[\"${p}\"]}'"
  ]) : null
}

output "bucket" {
  description = "Private bucket for bundles, tools and results."
  value       = aws_s3_bucket.this.id
}

output "bundle_keys" {
  description = "Where s3-mode hosts look for their bundle."
  value       = { for k, env in local.host_env : k => "s3://${aws_s3_bucket.this.id}/${env.BUNDLE_KEY}" if local.hosts[k].image_source == "s3" }
}

output "ssm_parameter_prefix" {
  description = "Generated secrets (read with aws ssm get-parameter --with-decryption)."
  value       = local.param_prefix
}

output "ttl_stop_at" {
  description = "UTC time at which the scheduler stops every host."
  value       = local.ttl_stop_at
}

output "network" {
  value = {
    vpc_id             = aws_vpc.this.id
    subnets            = local.subnet_ids
    nat_gateway        = var.enable_nat_gateway
    ssm_endpoints      = local.ssm_endpoints
    s3_endpoint_id     = aws_vpc_endpoint.s3.id
    s3_prefix_list_id  = aws_vpc_endpoint.s3.prefix_list_id
    flow_log_group     = var.enable_flow_logs ? aws_cloudwatch_log_group.flow_logs[0].name : null
    isolated_subnet_id = aws_subnet.isolated.id
  }
}

output "vcpus" {
  description = "vCPUs counted against the standard and G/VT quotas."
  value       = { standard = local.standard_vcpus, gpu = local.gpu_family_vcpu }
}
