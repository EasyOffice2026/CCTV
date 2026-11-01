output "url" {
  description = "The dashboard; also the cloud URL for edge boxes (CCTV_EDGE_CLOUD_URL) and the ops system."
  value       = local.web_url
}

output "load_balancer_dns_name" {
  description = "Point domain_name here (CNAME) when Route 53 isn't used."
  value       = aws_lb.main.dns_name
}

output "outbound_ips" {
  description = "The containers' public addresses (NAT), for allow-lists of webhook receivers or the ops system."
  value       = aws_eip.nat[*].public_ip
}

output "media_bucket" {
  value = aws_s3_bucket.media.id
}

output "face_key_alias" {
  value = aws_kms_alias.face.name
}

output "secret_names" {
  description = "Generated secrets in Secrets Manager (read them there, not from the state)."
  value       = [for s in aws_secretsmanager_secret.generated : s.name]
}

output "log_groups" {
  value = { for k, g in aws_cloudwatch_log_group.app : k => g.name }
}

# Used by deploy.sh.
output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "migrate_task_family" {
  value = aws_ecs_task_definition.migrate.family
}

output "private_subnets" {
  value = aws_subnet.private[*].id
}

output "app_security_group" {
  value = aws_security_group.app.id
}

output "migrate_command" {
  description = "Runs the database bootstrap and migrations once (deploy.sh does this for you)."
  value = join(" ", [
    "aws ecs run-task --region ${var.region} --cluster ${aws_ecs_cluster.main.name}",
    "--launch-type FARGATE --task-definition ${aws_ecs_task_definition.migrate.family}",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.private[*].id)}],securityGroups=[${aws_security_group.app.id}],assignPublicIp=DISABLED}'",
  ])
}

output "exec_command" {
  description = "A shell in an API container (e.g. for `python -m cctv_cloud.cli security-check --online` or create-superadmin)."
  value       = "aws ecs execute-command --region ${var.region} --cluster ${aws_ecs_cluster.main.name} --task <task id from: aws ecs list-tasks --cluster ${aws_ecs_cluster.main.name} --service-name api> --container api --interactive --command sh"
}

output "region" {
  value = var.region
}
