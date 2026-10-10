# ── S3 gateway endpoint (free) ──
# Attached to the private and isolated route tables (not the public one, so connected hosts
# keep their normal S3 path). Its policy is the zero-egress boundary for S3: hosts behind it
# can reach the testbed bucket and, optionally, the read-only AWS buckets listed below,
# and no other bucket.

locals {
  s3_endpoint_aws_buckets = concat(
    # Amazon Linux 2023 package repositories (dnf install docker on hosts without internet).
    var.s3_endpoint_allow_al2023_repos ? ["al2023-repos-${var.region}-de612dc2"] : [],
    # SSM Agent update packages.
    ["amazon-ssm-${var.region}", "amazon-ssm-packages-${var.region}"],
  )
}

# The testbed bucket statement names principals "*" and narrows them with aws:PrincipalAccount.
data "aws_iam_policy_document" "s3_endpoint" {
  # checkov:skip=CKV_AWS_283: the testbed bucket statement is narrowed to this account; the other allows only s3:GetObject on named AWS-owned buckets
  statement {
    sid = "TestbedBucket"
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  # dnf and the SSM Agent updater read these AWS-owned buckets anonymously (unsigned requests carry
  # no aws:PrincipalAccount), so this statement has no account condition: with one, every package
  # download got 403 and isolated hosts could not install Docker. It only allows reading objects
  # from the named buckets, which cannot carry data out.
  statement {
    sid = "AwsReadOnlyBuckets"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:GetObject"]
    resources = [for b in local.s3_endpoint_aws_buckets : "arn:${data.aws_partition.current.partition}:s3:::${b}/*"]
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id, aws_route_table.isolated.id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json
}

# ── SSM interface endpoints (about $0.012/h each in eu-central-1, plus $0.01/GB) ──
# Placed in the isolated subnet with private DNS, so every host in the VPC resolves
# ssm/ssmmessages/ec2messages to these ENIs. Toggle with enable_ssm_endpoints
# (null = on only while some host runs without internet access).

resource "aws_security_group" "endpoints" {
  name        = "${local.prefix}-endpoints"
  description = "SSM interface endpoints: HTTPS from inside the VPC only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  security_group_id = aws_security_group.endpoints.id
  description       = "HTTPS from the VPC"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = local.ssm_endpoints ? toset(["ssm", "ssmmessages", "ec2messages"]) : toset([])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.isolated.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
}
