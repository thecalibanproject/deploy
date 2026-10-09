# One VPC, one AZ, three subnets:
#   public    route to the internet gateway. Hosts get a public IPv4 but no inbound rule
#             allows anything from outside the VPC; access is SSM Session Manager only.
#   private   no internet route unless enable_nat_gateway = true.
#   isolated  no internet gateway route and no NAT, ever. Reaches only the VPC, the SSM
#             interface endpoints and S3 (gateway endpoint, policy-restricted).

# Flow logs are a toggle (enable_flow_logs, flowlogs.tf); scenario (d) turns them on.
#trivy:ignore:AWS-0178
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required for the interface endpoints' private DNS
}

# Lock down the VPC's default security group: no rules at all.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.subnet_cidrs.public
  availability_zone       = local.az
  map_public_ip_on_launch = false # set per instance instead
}

resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.subnet_cidrs.private
  availability_zone = local.az
}

resource "aws_subnet" "isolated" {
  vpc_id            = aws_vpc.this.id
  cidr_block        = local.subnet_cidrs.isolated
  availability_zone = local.az
}

# ── route tables ──

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# Isolated: only the implicit local route and the S3 gateway endpoint's prefix-list route.
resource "aws_route_table" "isolated" {
  vpc_id = aws_vpc.this.id
}

resource "aws_route_table_association" "isolated" {
  subnet_id      = aws_subnet.isolated.id
  route_table_id = aws_route_table.isolated.id
}

# ── optional NAT for the private subnet ──

resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? 1 : 0
  domain = "vpc"
}

resource "aws_nat_gateway" "this" {
  count         = var.enable_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public.id

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route" "private_nat" {
  count                  = var.enable_nat_gateway ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

locals {
  subnet_ids = {
    public   = aws_subnet.public.id
    private  = aws_subnet.private.id
    isolated = aws_subnet.isolated.id
  }
}
