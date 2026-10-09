terraform {
  # OpenTofu 1.8+ (also works with Terraform 1.6+). Validated with OpenTofu 1.13.
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.68"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.4"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.14"
    }
    cloudinit = {
      source  = "hashicorp/cloudinit"
      version = "~> 2.4"
    }
  }

  # Exact provider versions are pinned in .terraform.lock.hcl (committed).
  # State is local (terraform.tfstate in this directory, git-ignored) by default.
  # It contains the generated testbed secrets, so keep it private.
  # For a shared S3 backend, see backend.s3.tf.example and README.md "State".
}
