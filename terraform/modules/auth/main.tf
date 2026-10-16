variable "domain_name" { type = string }
variable "cognito_domain_prefix" { type = string }

locals {
  site_url = "https://${var.domain_name}"
}

resource "aws_cognito_user_pool" "vaad" {
  name = "vaad"
  # Passkeys as a first-class sign-in factor and managed login v2 both require ESSENTIALS.
  user_pool_tier           = "ESSENTIALS"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  web_authn_configuration {
    relying_party_id  = var.domain_name
    user_verification = "required"
  }

  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD", "WEB_AUTHN"]
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
    string_attribute_constraints {
      min_length = 5
      max_length = 256
    }
  }
}

resource "aws_cognito_user_pool_domain" "vaad" {
  domain                = var.cognito_domain_prefix
  user_pool_id          = aws_cognito_user_pool.vaad.id
  managed_login_version = 2
}

resource "aws_cognito_managed_login_branding" "vaad" {
  user_pool_id                = aws_cognito_user_pool.vaad.id
  client_id                   = aws_cognito_user_pool_client.vaad.id
  use_cognito_provided_values = true
}

resource "aws_cognito_user_pool_client" "vaad" {
  name         = "vaad-web"
  user_pool_id = aws_cognito_user_pool.vaad.id

  # Public client + PKCE: no secret to store, rotate, or leak into state.
  generate_secret                      = false
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.site_url}/auth/callback"]
  logout_urls                          = ["${local.site_url}/"]
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_AUTH"]
}

output "login_domain" { value = aws_cognito_user_pool_domain.vaad.domain }
output "user_pool_id" { value = aws_cognito_user_pool.vaad.id }
output "client_id" { value = aws_cognito_user_pool_client.vaad.id }
