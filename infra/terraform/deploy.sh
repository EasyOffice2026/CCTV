#!/usr/bin/env bash
# Deploy a release to an environment: the database first (with the new image), then the
# containers. Needs terraform, the AWS CLI and credentials for the account.
#
#   ./deploy.sh staging v1.2.0
#
# Reads <environment>.tfvars and backend-<environment>.hcl next to this script (see
# README.md). Also right after the first `terraform apply`, to create the tables.
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
[[ $# -eq 2 ]] || die "usage: $0 <staging|production> <release tag, e.g. v1.2.0>"
env=$1
tag=$2
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ -f $env.tfvars ]] || die "$env.tfvars is missing (copy $env.tfvars.example)"
[[ -f backend-$env.hcl ]] || die "backend-$env.hcl is missing (copy backend.hcl.example)"

terraform init -input=false -reconfigure -backend-config="backend-$env.hcl" >/dev/null
vars=(-input=false -var-file="$env.tfvars" -var "image_tag=$tag")

echo "==> 1/3 migrate task definition for $tag"
terraform apply "${vars[@]}" -target=aws_ecs_task_definition.migrate -auto-approve >/dev/null

out() { terraform output -raw "$1"; }
region=$(out region)
cluster=$(out cluster)
subnets=$(terraform output -json private_subnets | tr -d '[]" \n')
network="awsvpcConfiguration={subnets=[$subnets],securityGroups=[$(out app_security_group)],assignPublicIp=DISABLED}"

echo "==> 2/3 database: app role and migrations"
task=$(aws ecs run-task --region "$region" --cluster "$cluster" --launch-type FARGATE \
    --task-definition "$(out migrate_task_family)" --network-configuration "$network" \
    --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --region "$region" --cluster "$cluster" --tasks "$task"
code=$(aws ecs describe-tasks --region "$region" --cluster "$cluster" --tasks "$task" \
    --query 'tasks[0].containers[0].exitCode' --output text)
[[ $code == 0 ]] || die "migrations failed (exit $code); nothing else was changed. Logs: $(terraform output -json log_groups | tr -d '{}"' | tr ',' '\n' | grep migrate)"

echo "==> 3/3 containers (review the plan, then confirm)"
terraform apply "${vars[@]}"
echo "Deployed $tag to $env: $(out url)"
