# Plan-time checks (warnings, evaluated offline from the variables).

check "standard_vcpu_quota" {
  assert {
    condition     = local.standard_vcpus <= var.standard_vcpu_quota
    error_message = "CPU hosts need ${local.standard_vcpus} standard vCPUs, above standard_vcpu_quota = ${var.standard_vcpu_quota}. Lower router_count or the instance sizes, or raise the quota (README.md \"Service quotas\")."
  }
}

check "gpu_vcpu_quota" {
  assert {
    condition     = local.gpu_family_vcpu <= var.gpu_vcpu_quota
    error_message = "The GPU host needs ${local.gpu_family_vcpu} G/VT vCPUs, above gpu_vcpu_quota = ${var.gpu_vcpu_quota}."
  }
}

check "az_offers_instance_types" {
  assert {
    condition     = var.availability_zone != null || length(local.az_candidates) > 0
    error_message = "No AZ in ${var.region} offers both ${var.gpu_instance_type} and ${local.cpu_types.gateway}; set availability_zone or change the instance types."
  }
}

check "env_values_are_shell_safe" {
  assert {
    condition     = alltrue(flatten([for k, env in local.host_env : [for v in values(env) : !strcontains(v, "'")]]))
    error_message = "Variable values passed to the hosts must not contain single quotes."
  }
}
