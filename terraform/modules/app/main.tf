variable "bucket_id" { type = string }
variable "bucket_arn" { type = string }
variable "region" { type = string }
variable "site_url" { type = string }
variable "cognito_domain" { type = string }
variable "client_id" { type = string }
variable "user_pool_id" { type = string }
variable "hmac_param" { type = string }
variable "session_ttl" { type = number }
variable "layers_dir" { type = string }
variable "source_dir" { type = string }

resource "aws_lambda_layer_version" "deps" {
  layer_name               = "vaad-deps"
  filename                 = "${var.layers_dir}/deps.zip"
  source_code_hash         = filebase64sha256("${var.layers_dir}/deps.zip")
  compatible_runtimes      = ["python3.13"]
  compatible_architectures = ["arm64"]
}

data "archive_file" "app" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.build/app.zip"
  excludes    = ["__pycache__"]
}

resource "aws_iam_role" "app" {
  name = "vaad-app"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_caller_identity" "current" {}

# No s3:DeleteObject anywhere: receipts and database versions are never removed.
resource "aws_iam_role_policy" "app" {
  name = "vaad-app"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${var.bucket_arn}/*"
      },
      {
        # Without it a missing key reads as 403 instead of 404, and the first
        # run cannot tell "no database yet" from "not allowed".
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = var.bucket_arn
      },
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.hmac_param}"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/aws/lambda/vaad-app"
  retention_in_days = 30
}

resource "aws_lambda_function" "app" {
  function_name    = "vaad-app"
  role             = aws_iam_role.app.arn
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  handler          = "main.handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  memory_size      = 512
  timeout          = 15
  layers           = [aws_lambda_layer_version.deps.arn]
  # Caps the bill if the URL is ever hammered; one person never needs more.
  reserved_concurrent_executions = 5

  environment {
    variables = {
      BUCKET         = var.bucket_id
      DATA_DIR       = "/tmp/vaad"
      COGNITO_DOMAIN = var.cognito_domain
      CLIENT_ID      = var.client_id
      USER_POOL_ID   = var.user_pool_id
      REGION         = var.region
      SITE_URL       = var.site_url
      HMAC_PARAM     = var.hmac_param
      SESSION_TTL    = tostring(var.session_ttl)
    }
  }

  depends_on = [aws_cloudwatch_log_group.app]
}

resource "aws_lambda_function_url" "app" {
  function_name      = aws_lambda_function.app.function_name
  authorization_type = "AWS_IAM"
}

output "function_name" { value = aws_lambda_function.app.function_name }
output "function_url_domain" {
  value = replace(replace(aws_lambda_function_url.app.function_url, "https://", ""), "/", "")
}
