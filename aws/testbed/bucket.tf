# Private bucket for offline bundles, static tools and bench results.
#   bundles/<arch>/caliban-bundle-<v>.tar (+ .tar.sha256)   airgap/bundle.sh output
#   tools/<arch>/{docker-compose,minisign,cosign,caliban-bench}   for hosts without internet
#   results/<scenario>/...                                   bench output (results_upload = true)
# Per-bucket settings only; nothing account-wide is changed.

# No server access logging: a short-lived testbed bucket with no public access.
#trivy:ignore:AWS-0089
resource "aws_s3_bucket" "this" {
  # checkov:skip=CKV_AWS_18: no access logging for a short-lived private testbed bucket
  # checkov:skip=CKV_AWS_144: no cross-region replication; bundles can be rebuilt
  # checkov:skip=CKV_AWS_145: SSE-S3 as specified; no KMS key to manage
  # checkov:skip=CKV2_AWS_62: no event notifications needed
  bucket_prefix = "${local.prefix}-"
  force_destroy = var.bucket_force_destroy
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 on purpose: no KMS key to manage or pay for in a short-lived testbed.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3
    }
  }
}

# Bundles are tens of GB: do not keep old versions around for long.
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.this]
}
