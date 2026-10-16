variable "region" {
  description = "AWS region for all regional resources. CloudFront certs must be us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Bucket holding the database file and the PDFs. Created here."
  type        = string
}

variable "domain_name" {
  description = "Hostname the app is served on. Also the WebAuthn relying-party ID."
  type        = string
}

variable "cognito_domain_prefix" {
  description = "Prefix for the free Cognito-hosted login domain."
  type        = string
}

variable "hmac_param" {
  description = "SSM SecureString signing the session cookie. Written out of band by bootstrap-keys.sh."
  type        = string
  default     = "/vaad/session/hmac-key"
}

variable "session_ttl" {
  description = "Session cookie lifetime in seconds."
  type        = number
  default     = 604800
}

variable "budget_email" {
  description = "Where the monthly cost alert goes. Empty skips the budget."
  type        = string
  default     = ""
}
