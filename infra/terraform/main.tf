locals {
  name       = "${var.project}-${var.environment}"
  production = var.environment == "production"
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
  account_id = data.aws_caller_identity.current.account_id
  web_url    = "https://${var.domain_name}"

  # Settings every cloud container gets (cloud/cctv_cloud/config.py). Secrets are added
  # from Secrets Manager in ecs.tf; nothing secret is in a task definition in clear.
  cloud_environment = merge(
    {
      CCTV_ENVIRONMENT     = var.environment
      CCTV_WEB_URL         = local.web_url
      CCTV_S3_BUCKET       = aws_s3_bucket.media.id
      CCTV_S3_REGION       = var.region
      CCTV_S3_ENDPOINT_URL = local.s3_endpoint
      CCTV_FACE_KEY        = "aws-kms:${aws_kms_alias.face.name}"
      CCTV_ONCALL_EMAILS   = jsonencode(var.oncall_emails)
    },
    var.extra_environment,
  )
  s3_endpoint = "https://s3.${var.region}.amazonaws.com"

  cloud_secrets = merge(
    {
      CCTV_SECRET_KEY             = aws_secretsmanager_secret.generated["CCTV_SECRET_KEY"].arn
      CCTV_DATABASE_URL           = aws_secretsmanager_secret.generated["CCTV_DATABASE_URL"].arn
      CCTV_MIGRATION_DATABASE_URL = aws_secretsmanager_secret.generated["CCTV_MIGRATION_DATABASE_URL"].arn
      CCTV_REDIS_URL              = aws_secretsmanager_secret.generated["CCTV_REDIS_URL"].arn
      CCTV_METRICS_TOKEN          = aws_secretsmanager_secret.generated["CCTV_METRICS_TOKEN"].arn
    },
    var.secret_environment,
  )
}
