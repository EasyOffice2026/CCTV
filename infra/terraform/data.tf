# PostgreSQL (RDS), Redis (ElastiCache), the media bucket (S3) and the key for face
# templates (KMS). All encrypted at rest; database and Redis only over TLS (docs/security.md).

# --- PostgreSQL ------------------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = local.name
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl" # no plain-text connections
    value = "1"
  }
}

# Owner role (migrations, the worker's system jobs) and the API's role, which can't bypass
# row-level security (created by `cli bootstrap-db` in the migrate task, ecs.tf).
# Letters and digits only: they go into connection URLs.
resource "random_password" "db_owner" {
  length  = 40
  special = false
}

resource "random_password" "db_app" {
  length  = 40
  special = false
}

resource "aws_db_instance" "main" {
  identifier                      = local.name
  engine                          = "postgres"
  engine_version                  = "16"
  auto_minor_version_upgrade      = true
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_allocated_storage
  max_allocated_storage           = var.db_max_allocated_storage
  storage_type                    = "gp3"
  storage_encrypted               = true # can't be turned on later
  db_name                         = "cctv"
  username                        = "cctv_owner"
  password                        = random_password.db_owner.result
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.data.id]
  parameter_group_name            = aws_db_parameter_group.main.name
  publicly_accessible             = false
  multi_az                        = local.production
  backup_retention_period         = 14
  backup_window                   = "23:00-23:30" # 02:00 in Kuwait
  maintenance_window              = "fri:00:00-fri:00:30"
  copy_tags_to_snapshot           = true
  deletion_protection             = local.production
  skip_final_snapshot             = !local.production
  final_snapshot_identifier       = local.production ? "${local.name}-final" : null
  apply_immediately               = !local.production
  ca_cert_identifier              = "rds-ca-rsa2048-g1"
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

locals {
  db_host = aws_db_instance.main.address
  # sslmode=require: encrypted (rds.force_ssl). verify-full also checks the server's
  # certificate but needs the RDS CA bundle in the image.
  db_url_owner = "postgresql+psycopg://cctv_owner:${random_password.db_owner.result}@${local.db_host}:5432/cctv?sslmode=require"
  db_url_app   = "postgresql+psycopg://cctv_app:${random_password.db_app.result}@${local.db_host}:5432/cctv?sslmode=require"
}

# --- Redis (live events, the relay between API containers) ------------------------------------

resource "random_password" "redis" {
  length  = 40
  special = false
}

resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id       = local.name
  description                = "CCTV live events and API relay"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = local.production ? 2 : 1
  automatic_failover_enabled = local.production
  multi_az_enabled           = local.production
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.data.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  apply_immediately          = !local.production
}

locals {
  redis_url = "rediss://:${random_password.redis.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
}

# --- media bucket (event snapshots and clips, report files) -----------------------------------

resource "aws_s3_bucket" "media" {
  bucket        = "${local.name}-media-${local.account_id}"
  force_destroy = !local.production
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    id     = "backstop-expiry"
    status = "Enabled"
    filter {}
    expiration {
      days = var.media_expiry_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Browsers open pre-signed links from the dashboard (downloads use fetch).
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = [local.web_url]
    max_age_seconds = 3600
  }
}

data "aws_iam_policy_document" "media_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.media.arn, "${aws_s3_bucket.media.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "media" {
  bucket     = aws_s3_bucket.media.id
  policy     = data.aws_iam_policy_document.media_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.media]
}

# --- key for face templates (plan 7.2; envelope encryption, cloud/services/face_keys.py) ------

resource "aws_kms_key" "face" {
  description             = "${local.name} face templates"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "face" {
  name          = "alias/${local.name}-face"
  target_key_id = aws_kms_key.face.key_id
}

# --- secrets the containers read (values generated here) --------------------------------------

resource "random_password" "secret_key" {
  length  = 64
  special = false
}

resource "random_password" "metrics_token" {
  length  = 40
  special = false
}

locals {
  generated_secret_names = toset([
    "CCTV_SECRET_KEY",
    "CCTV_DATABASE_URL",
    "CCTV_MIGRATION_DATABASE_URL",
    "CCTV_REDIS_URL",
    "CCTV_METRICS_TOKEN",
  ])
  generated_secrets = {
    CCTV_SECRET_KEY             = random_password.secret_key.result
    CCTV_DATABASE_URL           = local.db_url_app
    CCTV_MIGRATION_DATABASE_URL = local.db_url_owner
    CCTV_REDIS_URL              = local.redis_url
    CCTV_METRICS_TOKEN          = random_password.metrics_token.result
  }
}

resource "aws_secretsmanager_secret" "generated" {
  for_each                = local.generated_secret_names
  name                    = "${var.project}/${var.environment}/${each.key}"
  recovery_window_in_days = local.production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = local.generated_secret_names
  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = local.generated_secrets[each.key]
}
