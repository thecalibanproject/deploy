provider "aws" {
  region  = var.region
  profile = var.aws_profile

  # Optional guard for a shared account: refuse to run against any other account id.
  # Set it in terraform.tfvars (git-ignored), never in committed code.
  allowed_account_ids = var.allowed_account_ids

  # No default_tags: resources are identified by the caliban-testbed name prefix and by
  # the ids in this module's outputs.
}
