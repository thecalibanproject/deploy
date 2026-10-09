# ── instance role: SSM Session Manager + read access to the testbed bucket ──

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.prefix}-instance"
  description        = "Caliban testbed hosts: SSM managed instance + testbed bucket read"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Also grants ssm:GetParameter(s), which the hosts use to read the generated secrets
# under /caliban-testbed/ (SecureString, AWS managed aws/ssm key).
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "bucket_access" {
  statement {
    sid       = "ListTestbedBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.this.arn]
  }

  statement {
    sid       = "ReadTestbedBucket"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.this.arn}/*"]
  }

  dynamic "statement" {
    for_each = var.results_upload ? [1] : []
    content {
      sid       = "WriteBenchResults"
      actions   = ["s3:PutObject"]
      resources = ["${aws_s3_bucket.this.arn}/results/*"]
    }
  }
}

resource "aws_iam_role_policy" "bucket_access" {
  name   = "${local.prefix}-bucket"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.bucket_access.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.prefix}-instance"
  role = aws_iam_role.instance.name
}
