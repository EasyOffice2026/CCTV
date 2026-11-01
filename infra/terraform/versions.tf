# The cloud side of the CCTV system on AWS (plan 1.4): network, PostgreSQL, Redis, the media
# bucket, the face-template key, secrets, the containers (ECS Fargate) and the load balancer
# with TLS. One state per environment (staging, production); see README.md.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Settings in backend.hcl (see backend.hcl.example): an encrypted S3 bucket per account.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}
