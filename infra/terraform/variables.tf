variable "project" {
  description = "Prefix of every resource name."
  type        = string
  default     = "cctv"
}

variable "environment" {
  description = "staging or production. Production gets a standby database and Redis, two NAT gateways, deletion protection and a final database snapshot."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "region" {
  description = "AWS region. me-central-1 is the UAE; me-south-1 is Bahrain."
  type        = string
  default     = "me-central-1"
}

# --- address and certificate ---------------------------------------------------------------

variable "domain_name" {
  description = "The dashboard's address, e.g. cctv.example.com (also what edge boxes and the ops system use)."
  type        = string
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone of domain_name: the certificate is validated and the address pointed at the load balancer automatically. Empty: give certificate_arn and point the DNS yourself (output load_balancer_dns_name)."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "An issued ACM certificate for domain_name in this region, when route53_zone_id is empty."
  type        = string
  default     = ""

  validation {
    condition     = var.certificate_arn != "" || var.route53_zone_id != ""
    error_message = "Give route53_zone_id (the certificate is made and validated for you) or certificate_arn."
  }
}

# --- images ----------------------------------------------------------------------------------

variable "image_registry" {
  description = "Where the release workflow pushes the images (cctv-api, cctv-web)."
  type        = string
  default     = "ghcr.io/easyoffice2026"
}

variable "image_tag" {
  description = "The release to run, e.g. v1.2.0 (a tag from .github/workflows/release.yml). Change it and apply to deploy."
  type        = string
}

variable "registry_credentials_secret_arn" {
  description = "Secrets Manager secret {\"username\": ..., \"password\": <GitHub token with read:packages>} when the GHCR packages are private. Empty: public images."
  type        = string
  default     = ""
}

# --- sizes -----------------------------------------------------------------------------------

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage" {
  description = "GB; grows by itself up to db_max_allocated_storage."
  type        = number
  default     = 50
}

variable "db_max_allocated_storage" {
  type    = number
  default = 500
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "api_count" {
  description = "API containers (2 or more, in different zones)."
  type        = number
  default     = 2
}

variable "web_count" {
  type    = number
  default = 2
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "worker_cpu" {
  type    = number
  default = 512
}

variable "worker_memory" {
  type    = number
  default = 1024
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "media_expiry_days" {
  description = "Backstop: objects older than this are deleted by the bucket itself. The worker deletes media much earlier, per client's retention (plan §6)."
  type        = number
  default     = 400
}

# --- application settings --------------------------------------------------------------------

variable "oncall_emails" {
  description = "Told about boxes offline, streams down, disks filling (plan 11.4)."
  type        = list(string)
  default     = []
}

variable "embed_ancestors" {
  description = "Origins of the operational system that may frame the /embed/ pages (plan 8), space-separated."
  type        = string
  default     = ""
}

variable "extra_environment" {
  description = "More CCTV_* settings for the API and the worker, e.g. {CCTV_WHATSAPP_PHONE_NUMBER_ID = \"...\"} (not secrets)."
  type        = map(string)
  default     = {}
}

variable "secret_environment" {
  description = "Settings that are secrets, created by hand in Secrets Manager (so their values never enter the Terraform state): {CCTV_VAPID_PRIVATE_KEY = \"<secret ARN>\", CCTV_WHATSAPP_ACCESS_TOKEN = \"<ARN>\", CCTV_ONCALL_WEBHOOK_URL = \"<ARN>\", ...}."
  type        = map(string)
  default     = {}
}
