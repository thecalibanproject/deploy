# Auto-stop safeguard. Two layers, so a forgotten GPU cannot burn money for days:
#
# 1. EventBridge Scheduler, one-shot: ttl_hours after the current set of hosts was created,
#    it calls ec2:StopInstances on their explicit instance ids. The role it uses may stop
#    exactly those instances and nothing else. Re-arm (extend) with
#      tofu apply -replace=time_static.ttl
# 2. On-demand hosts also get a systemd timer that powers the OS off ttl_hours after every
#    boot (instance_initiated_shutdown_behavior = stop). Spot hosts rely on layer 1.
#
# Stopped hosts still pay for their EBS volumes (and the public IPv4 is released). Run
# `tofu destroy` to stop paying for anything (README.md "Teardown").

locals {
  instance_ids  = [for k in sort(keys(local.hosts)) : aws_instance.host[k].id]
  instance_arns = [for k in sort(keys(local.hosts)) : aws_instance.host[k].arn]
}

resource "time_static" "ttl" {
  count = length(local.hosts) > 0 ? 1 : 0

  triggers = {
    instance_ids = join(",", local.instance_ids)
  }
}

locals {
  ttl_stop_at = length(time_static.ttl) > 0 ? timeadd(time_static.ttl[0].rfc3339, "${var.ttl_hours}h") : null
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "autostop" {
  count = length(local.hosts) > 0 ? 1 : 0

  name               = "${local.prefix}-autostop"
  description        = "EventBridge Scheduler: stop the Caliban testbed hosts after ttl_hours"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "autostop" {
  count = length(local.hosts) > 0 ? 1 : 0

  statement {
    sid       = "StopTestbedHostsOnly"
    actions   = ["ec2:StopInstances"]
    resources = local.instance_arns
  }
}

resource "aws_iam_role_policy" "autostop" {
  count = length(local.hosts) > 0 ? 1 : 0

  name   = "${local.prefix}-autostop"
  role   = aws_iam_role.autostop[0].id
  policy = data.aws_iam_policy_document.autostop[0].json
}

resource "aws_scheduler_schedule" "ttl_stop" {
  count = length(local.hosts) > 0 ? 1 : 0

  # checkov:skip=CKV_AWS_297: the schedule input holds only instance ids; no CMK needed
  name        = "${local.prefix}-ttl-stop"
  description = "Stop the Caliban testbed hosts ${var.ttl_hours}h after they were created"

  schedule_expression          = "at(${formatdate("YYYY-MM-DD'T'hh:mm:ss", local.ttl_stop_at)})"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:${data.aws_partition.current.partition}:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.autostop[0].arn
    input    = jsonencode({ InstanceIds = local.instance_ids })

    retry_policy {
      maximum_retry_attempts       = 10
      maximum_event_age_in_seconds = 3600
    }
  }

  depends_on = [aws_iam_role_policy.autostop]
}
