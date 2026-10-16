variable "domain_name" { type = string }
variable "app_function_url_domain" { type = string }

resource "aws_acm_certificate" "vaad" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# DNS lives at the registrar, so validation is a manual record. Terraform waits here.
resource "aws_acm_certificate_validation" "vaad" {
  certificate_arn = aws_acm_certificate.vaad.arn
}

resource "aws_cloudfront_origin_access_control" "lambda" {
  name                              = "vaad-app-lambda"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

locals {
  app_origin = "app-lambda"
  # Managed policies: CachingDisabled, AllViewerExceptHostHeader.
  cache_disabled         = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
  all_viewer_except_host = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
}

# Everything goes to the app, which checks the session itself; the function
# URL only accepts requests CloudFront signed.
resource "aws_cloudfront_distribution" "vaad" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "vaad bait"
  aliases         = [var.domain_name]
  price_class     = "PriceClass_100"

  origin {
    origin_id                = local.app_origin
    domain_name              = var.app_function_url_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id         = local.app_origin
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = local.cache_disabled
    origin_request_policy_id = local.all_viewer_except_host
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.vaad.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

output "distribution_arn" { value = aws_cloudfront_distribution.vaad.arn }
output "distribution_domain" { value = aws_cloudfront_distribution.vaad.domain_name }
output "acm_validation" {
  value = { for o in aws_acm_certificate.vaad.domain_validation_options : o.domain_name => {
    name  = o.resource_record_name
    type  = o.resource_record_type
    value = o.resource_record_value
  } }
}
