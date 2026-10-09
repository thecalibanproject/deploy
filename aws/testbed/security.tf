# Security groups. Nothing is open to 0.0.0.0/0 inbound: there is no SSH and access is
# SSM Session Manager only (an outbound connection from the SSM Agent).
#
#   hosts           every host. Ingress: the service ports below, from the VPC CIDR only.
#                   Egress: the VPC CIDR and S3 (gateway endpoint prefix list) only.
#   internet-egress added to hosts in a connected tier (public, or private with NAT):
#                   HTTP/HTTPS out for git clones, image pulls, crates, Hugging Face and SSM.

locals {
  ingress_ports = {
    data-plane    = { from = 8080, to = 8080, desc = "Caliban data plane (/v1, /healthz)" }
    control-plane = { from = 8081, to = 8081, desc = "Caliban control plane, console, split-mode snapshots" }
    postgres      = { from = 5432, to = 5432, desc = "Postgres on the gateway" }
    valkey        = { from = 6379, to = 6379, desc = "Valkey on the gateway (shared quota store)" }
    qdrant        = { from = 6333, to = 6334, desc = "Qdrant HTTP and gRPC on the gateway" }
    models        = { from = 8000, to = 8003, desc = "vLLM chat (8000), TEI embed (8001), vLLM rerank (8002), gpt-oss (8003) on the GPU host" }
    mock-upstream = { from = 9000, to = 9001, desc = "Mock upstream served by the load generator" }
  }
}

resource "aws_security_group" "hosts" {
  # checkov:skip=CKV2_AWS_5: attached to every aws_instance.host (for_each)
  name        = "${local.prefix}-hosts"
  description = "Caliban testbed hosts: service ports from inside the VPC only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "hosts" {
  for_each = local.ingress_ports

  security_group_id = aws_security_group.hosts.id
  description       = each.value.desc
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = each.value.from
  to_port           = each.value.to
}

resource "aws_vpc_security_group_egress_rule" "hosts_vpc" {
  security_group_id = aws_security_group.hosts.id
  description       = "Anything inside the VPC (other hosts, SSM endpoints, DNS resolver)"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "hosts_s3" {
  security_group_id = aws_security_group.hosts.id
  description       = "S3 through the gateway endpoint (policy limits it to the testbed bucket)"
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_security_group" "internet_egress" {
  # checkov:skip=CKV2_AWS_5: attached to connected aws_instance.host entries
  name        = "${local.prefix}-internet-egress"
  description = "Outbound HTTP/HTTPS for hosts in a connected tier; no inbound"
  vpc_id      = aws_vpc.this.id
}

# Only attached to hosts in a connected tier (never isolated ones): they clone from GitHub and
# pull images, crates and weights from CDNs whose ranges are not fixed.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "internet_https" {
  security_group_id = aws_security_group.internet_egress.id
  description       = "HTTPS out (GitHub, container registries, crates.io, Hugging Face, SSM)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "internet_http" {
  security_group_id = aws_security_group.internet_egress.id
  description       = "HTTP out (Ubuntu apt mirrors on the GPU host)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}
