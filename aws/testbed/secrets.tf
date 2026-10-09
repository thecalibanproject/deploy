# Testbed secrets, generated here and stored as SSM parameters under /<name_prefix>/.
# Hosts read them at boot with `aws ssm get-parameter --with-decryption` (allowed by
# AmazonSSMManagedInstanceCore). They are never in user data or in git, but they ARE in
# the OpenTofu state: keep the state private (README.md "State").

resource "random_password" "admin_token" {
  length  = 64
  special = false
}

resource "random_password" "postgres" {
  length  = 40
  special = false
}

resource "random_password" "valkey" {
  length  = 40
  special = false
}

resource "random_password" "router_token" {
  length  = 64
  special = false
}

# CALIBAN_KEK: exactly 32 random bytes, base64.
resource "random_bytes" "kek" {
  length = 32
}

# Split-mode snapshot signing key (Ed25519). The gateway derives the raw 32-byte seed for
# CALIBAN_SNAPSHOT_SIGNING_KEY from the PKCS#8 PEM; routers derive CALIBAN_SNAPSHOT_PUBLIC_KEY
# from the public PEM and never see the private key.
resource "tls_private_key" "snapshot" {
  algorithm = "ED25519"
}

locals {
  secure_params = {
    admin-token          = random_password.admin_token.result
    postgres-password    = random_password.postgres.result
    valkey-password      = random_password.valkey.result
    router-token         = random_password.router_token.result
    kek                  = random_bytes.kek.base64
    snapshot-signing-pem = tls_private_key.snapshot.private_key_pem_pkcs8
  }
}

resource "aws_ssm_parameter" "secret" {
  # checkov:skip=CKV_AWS_337: AWS managed aws/ssm key; a CMK would need kms:Decrypt beyond the SSM-only role
  for_each = nonsensitive(toset(keys(local.secure_params)))

  name        = "${local.param_prefix}/${each.key}"
  description = "Caliban testbed generated secret"
  type        = "SecureString"
  value       = local.secure_params[each.key]
}

resource "aws_ssm_parameter" "snapshot_public_pem" {
  # checkov:skip=CKV2_AWS_34: public key, readable by design
  name        = "${local.param_prefix}/snapshot-public-pem"
  description = "Caliban testbed snapshot verification key (public)"
  type        = "String"
  value       = tls_private_key.snapshot.public_key_pem
}
