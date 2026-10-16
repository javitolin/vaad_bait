variable "bucket_name" { type = string }

resource "aws_s3_bucket" "vaad" {
  bucket = var.bucket_name

  # Receipts and the ledger are records the building may need years later.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "vaad" {
  bucket                  = aws_s3_bucket.vaad.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Every database save becomes a version, so any bad write can be rolled back
# (to one of the last 30, see the lifecycle rule below).
resource "aws_s3_bucket_versioning" "vaad" {
  bucket = aws_s3_bucket.vaad.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Only the database file churns: keep its 30 newest previous versions. Receipts,
# vendor files and summaries are never pruned.
resource "aws_s3_bucket_lifecycle_configuration" "vaad" {
  bucket = aws_s3_bucket.vaad.id

  rule {
    id     = "db-keep-30-backups"
    status = "Enabled"
    filter {
      prefix = "db/"
    }
    noncurrent_version_expiration {
      noncurrent_days           = 1
      newer_noncurrent_versions = 30
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.vaad]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "vaad" {
  bucket = aws_s3_bucket.vaad.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "id" { value = aws_s3_bucket.vaad.id }
output "arn" { value = aws_s3_bucket.vaad.arn }
