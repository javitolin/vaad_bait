output "site_url" { value = local.site_url }

output "distribution_domain" {
  description = "CNAME target for the vaad record at the registrar."
  value       = module.cdn.distribution_domain
}

output "acm_validation_record" {
  description = "Add this CNAME at the registrar, then the cert issues."
  value       = module.cdn.acm_validation
}

output "login_domain" {
  value = "${module.auth.login_domain}.auth.${var.region}.amazoncognito.com"
}

output "user_pool_id" { value = module.auth.user_pool_id }
output "bucket" { value = module.storage.id }
