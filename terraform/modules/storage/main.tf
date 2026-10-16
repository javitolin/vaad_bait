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

# Every database save becomes a version, so any bad write can be rolled back.
# Old versions are kept: at this size they cost nothing.
resource "aws_s3_bucket_versioning" "vaad" {
  bucket = aws_s3_bucket.vaad.id
  versioning_configuration {
    status = "Enabled"
  }
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
