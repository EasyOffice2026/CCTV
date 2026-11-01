# The containers on ECS Fargate: api (2+, behind the load balancer), worker (exactly one),
# web (nginx with the dashboard) and migrate (a one-off task: deploy.sh runs it before each
# release). Images come from the release workflow.

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = toset(["api", "worker", "web", "migrate"])
  name              = "/${var.project}/${var.environment}/${each.key}"
  retention_in_days = var.log_retention_days
}

# --- roles --------------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Used by ECS to start containers: pull images, read the secrets, write logs.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = compact(concat(
      [for s in aws_secretsmanager_secret.generated : s.arn],
      values(var.secret_environment),
      [var.registry_credentials_secret_arn],
    ))
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Used by the application: the media bucket, the face key, and ECS Exec for support.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "MediaObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }
  statement {
    sid       = "MediaBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetEncryptionConfiguration"]
    resources = [aws_s3_bucket.media.arn]
  }
  statement {
    sid       = "FaceTemplates"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.face.arn]
  }
  statement {
    sid = "EcsExec" # `aws ecs execute-command` into a container, e.g. for security-check
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "app"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- task definitions ------------------------------------------------------------------------

locals {
  api_image = "${var.image_registry}/cctv-api:${var.image_tag}"
  web_image = "${var.image_registry}/cctv-web:${var.image_tag}"

  registry_auth = var.registry_credentials_secret_arn == "" ? {} : {
    repositoryCredentials = { credentialsParameter = var.registry_credentials_secret_arn }
  }
  cloud_env          = [for k in sort(keys(local.cloud_environment)) : { name = k, value = local.cloud_environment[k] }]
  cloud_secrets_list = [for k in sort(keys(local.cloud_secrets)) : { name = k, valueFrom = local.cloud_secrets[k] }]

  logs = {
    for c in ["api", "worker", "web", "migrate"] : c => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app[c].name
        awslogs-region        = var.region
        awslogs-stream-prefix = c
      }
    }
  }

  migrate_command = ["sh", "-c", "python -m cctv_cloud.cli bootstrap-db && alembic upgrade head"]

  cloud_container = merge(local.registry_auth, {
    image           = local.api_image
    essential       = true
    environment     = local.cloud_env
    secrets         = local.cloud_secrets_list
    linuxParameters = { initProcessEnabled = true } # for ECS Exec
  })
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(local.cloud_container, {
    name         = "api"
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    # Behind the load balancer (only it can reach the containers): take the client's
    # address from X-Forwarded-For, for the audit log and sign-in limits.
    environment      = concat(local.cloud_env, [{ name = "FORWARDED_ALLOW_IPS", value = "*" }])
    logConfiguration = local.logs["api"]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  })])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(local.cloud_container, {
    name             = "worker"
    command          = ["python", "-m", "cctv_cloud.worker", "run"]
    logConfiguration = local.logs["worker"]
  })])
}

# One-off: the API's database role, then the migrations (deploy.sh; output migrate_command).
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(local.cloud_container, {
    name             = "migrate"
    command          = local.migrate_command
    logConfiguration = local.logs["migrate"]
  })])
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${local.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(local.registry_auth, {
    name         = "web"
    image        = local.web_image
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment = [
      # The load balancer sends /v1 straight to the API, so nginx's proxy isn't used here.
      { name = "API_UPSTREAM", value = "http://127.0.0.1:8000" },
      # Pre-signed snapshot and clip links point here (Content-Security-Policy).
      { name = "MEDIA_ORIGIN", value = local.s3_endpoint },
      { name = "EMBED_ANCESTORS", value = var.embed_ancestors },
    ]
    logConfiguration = local.logs["web"]
  })])
}

# --- services --------------------------------------------------------------------------------

locals {
  service_network = {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_count
  launch_type                       = "FARGATE"
  enable_execute_command            = true
  health_check_grace_period_seconds = 60
  propagate_tags                    = "SERVICE"
  network_configuration {
    subnets          = local.service_network.subnets
    security_groups  = local.service_network.security_groups
    assign_public_ip = local.service_network.assign_public_ip
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "web" {
  name                              = "web"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.web.arn
  desired_count                     = var.web_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30
  propagate_tags                    = "SERVICE"
  network_configuration {
    subnets          = local.service_network.subnets
    security_groups  = local.service_network.security_groups
    assign_public_ip = local.service_network.assign_public_ip
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8080
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.https]
}

# Exactly one: alert retries, reports, retention, health checks and the till check must not
# run twice. Deploys stop the old one before starting the new one.
resource "aws_ecs_service" "worker" {
  name                               = "worker"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  enable_execute_command             = true
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  propagate_tags                     = "SERVICE"
  network_configuration {
    subnets          = local.service_network.subnets
    security_groups  = local.service_network.security_groups
    assign_public_ip = local.service_network.assign_public_ip
  }
}
