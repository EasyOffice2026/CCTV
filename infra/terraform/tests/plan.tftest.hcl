# `terraform test` (no AWS account needed: the providers are mocked). Checks what the plan
# would create for staging and production.

mock_provider "aws" {
  override_data {
    target = data.aws_availability_zones.available
    values = { names = ["me-central-1a", "me-central-1b", "me-central-1c"] }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

mock_provider "random" {}

variables {
  domain_name     = "cctv-staging.example.com"
  route53_zone_id = "Z0000000000000000000"
  image_tag       = "v1.0.0"
}

run "staging" {
  command = plan

  variables {
    environment = "staging"
  }

  assert {
    condition     = aws_db_instance.main.multi_az == false && aws_db_instance.main.deletion_protection == false
    error_message = "staging: one database instance, no deletion protection"
  }
  assert {
    condition     = aws_db_instance.main.storage_encrypted && !aws_db_instance.main.publicly_accessible
    error_message = "the database is encrypted and private"
  }
  assert {
    condition     = length(aws_nat_gateway.main) == 1 && length(aws_subnet.private) == 2
    error_message = "staging: one NAT gateway, two zones"
  }
  assert {
    condition     = aws_elasticache_replication_group.main.transit_encryption_enabled && aws_elasticache_replication_group.main.num_cache_clusters == 1
    error_message = "Redis over TLS, one node in staging"
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.media.block_public_acls && aws_s3_bucket_public_access_block.media.restrict_public_buckets
    error_message = "the media bucket is never public"
  }
  assert {
    condition     = toset(one(aws_lb_listener_rule.api.condition).path_pattern[0].values) == toset(["/v1/*", "/metrics"])
    error_message = "/v1 and /metrics go to the API"
  }
  assert {
    condition     = aws_lb_listener.https.ssl_policy == "ELBSecurityPolicy-TLS13-1-2-2021-06"
    error_message = "TLS 1.2+ only"
  }
  assert {
    condition     = aws_ecs_service.worker.desired_count == 1 && aws_ecs_service.worker.deployment_maximum_percent == 100
    error_message = "exactly one worker, also during deploys"
  }
  assert {
    condition     = length(aws_acm_certificate.main) == 1 && length(aws_route53_record.site) == 1
    error_message = "certificate and DNS through the Route 53 zone"
  }
  assert {
    condition     = local.cloud_environment["CCTV_WEB_URL"] == "https://cctv-staging.example.com" && local.cloud_environment["CCTV_ENVIRONMENT"] == "staging"
    error_message = "the API knows its address and environment"
  }
  assert {
    condition = alltrue([for k in ["CCTV_SECRET_KEY", "CCTV_DATABASE_URL", "CCTV_MIGRATION_DATABASE_URL", "CCTV_REDIS_URL", "CCTV_METRICS_TOKEN"] :
    contains(keys(local.cloud_secrets), k) && !contains(keys(local.cloud_environment), k)])
    error_message = "secrets come from Secrets Manager, never as plain settings in the task definition"
  }
  assert {
    condition     = local.migrate_command[2] == "python -m cctv_cloud.cli bootstrap-db && alembic upgrade head"
    error_message = "the migrate task creates the app role, then migrates"
  }
  assert {
    condition     = local.cloud_environment["CCTV_FACE_KEY"] == "aws-kms:alias/cctv-staging-face" && local.cloud_environment["CCTV_S3_ENDPOINT_URL"] == "https://s3.me-central-1.amazonaws.com"
    error_message = "face templates use the KMS key; media the regional S3 endpoint"
  }
}

run "production" {
  command = plan

  variables {
    environment = "production"
    domain_name = "cctv.example.com"
  }

  assert {
    condition     = aws_db_instance.main.multi_az && aws_db_instance.main.deletion_protection && aws_db_instance.main.final_snapshot_identifier == "cctv-production-final"
    error_message = "production: standby database, deletion protection, final snapshot"
  }
  assert {
    condition     = length(aws_nat_gateway.main) == 2
    error_message = "production: a NAT gateway per zone"
  }
  assert {
    condition     = aws_elasticache_replication_group.main.num_cache_clusters == 2 && aws_elasticache_replication_group.main.automatic_failover_enabled
    error_message = "production: Redis with a replica and failover"
  }
  assert {
    condition     = aws_lb.main.enable_deletion_protection && !aws_s3_bucket.media.force_destroy
    error_message = "production: the load balancer and bucket can't be deleted by accident"
  }
}

run "needs_a_certificate_source" {
  command = plan

  variables {
    environment     = "staging"
    route53_zone_id = ""
    certificate_arn = ""
  }

  expect_failures = [var.certificate_arn]
}
