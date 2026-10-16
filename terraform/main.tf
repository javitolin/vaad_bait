locals {
  site_url = "https://${var.domain_name}"
}

module "storage" {
  source      = "./modules/storage"
  bucket_name = var.bucket_name
}

module "auth" {
  source                = "./modules/auth"
  domain_name           = var.domain_name
  cognito_domain_prefix = var.cognito_domain_prefix
}

module "app" {
  source         = "./modules/app"
  bucket_id      = module.storage.id
  bucket_arn     = module.storage.arn
  region         = var.region
  site_url       = local.site_url
  cognito_domain = "${module.auth.login_domain}.auth.${var.region}.amazoncognito.com"
  client_id      = module.auth.client_id
  user_pool_id   = module.auth.user_pool_id
  hmac_param     = var.hmac_param
  session_ttl    = var.session_ttl
  layers_dir     = "${path.root}/../layers"
  source_dir     = "${path.root}/../app"
}

module "cdn" {
  source                  = "./modules/cdn"
  domain_name             = var.domain_name
  app_function_url_domain = module.app.function_url_domain
}

# Kept out of the modules because it needs the distribution ARN, which would
# otherwise close a cycle. OAC needs both actions granted: InvokeFunctionUrl
# alone yields AccessDeniedException before the handler ever runs, and only
# the URL action accepts function_url_auth_type.
resource "aws_lambda_permission" "cloudfront_url" {
  statement_id           = "AllowCloudFrontInvokeUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = module.app.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = module.cdn.distribution_arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id  = "AllowCloudFrontInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.app.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = module.cdn.distribution_arn
}

# Account-wide on purpose: it also covers the gallery, and a runaway cost
# anywhere in the account is worth hearing about.
resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "account-monthly"
  budget_type  = "COST"
  limit_amount = "5"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
