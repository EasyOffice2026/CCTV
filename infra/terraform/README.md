# Cloud infrastructure on AWS (Terraform)

Creates everything the cloud side needs in one AWS region (default `me-central-1`, UAE):

| Piece | What | Notes |
|---|---|---|
| Network | VPC over two zones; the load balancer in public subnets, everything else private | NAT for outbound traffic: one gateway in staging, one per zone in production |
| PostgreSQL 16 | RDS, encrypted, TLS only, 14 days of backups | Production: standby in the other zone, deletion protection, final snapshot |
| Redis 7 | ElastiCache, encrypted, TLS, password | Production: replica with failover |
| Media | S3 bucket, encrypted, never public, TLS only, objects deleted after 400 days as a backstop | The worker deletes media earlier, per client retention |
| Face templates | KMS key with yearly rotation (`CCTV_FACE_KEY=aws-kms:alias/...`) | |
| Secrets | Secrets Manager: secret key, database and Redis URLs, metrics token (generated) | Plus your own (`secret_environment`): VAPID, WhatsApp, on-call webhook |
| Containers | ECS Fargate: `api` (2), `worker` (exactly 1), `web` (2), `migrate` (one-off) | Images from the release workflow; ECS Exec for support |
| Entrance | Load balancer with TLS 1.2+, `https://<domain_name>`: `/v1` and `/metrics` to the API, the rest to the dashboard | Certificate and DNS through Route 53, or your own |

Rough monthly cost in me-central-1: staging about $250, production about $600 (mostly the
database, NAT gateways and load balancer); check with the AWS pricing calculator.

## Once per AWS account

1. An IAM user or SSO role for whoever applies Terraform (administrator for the first run).
2. A bucket for the Terraform state (it contains the generated database passwords):

   ```bash
   aws s3api create-bucket --bucket cctv-terraform-state-<account id> --region me-central-1 \
     --create-bucket-configuration LocationConstraint=me-central-1
   aws s3api put-bucket-versioning --bucket cctv-terraform-state-<account id> \
     --versioning-configuration Status=Enabled
   aws s3api put-public-access-block --bucket cctv-terraform-state-<account id> \
     --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   ```
   (S3 encrypts it by default.)
3. The domain in Route 53 (a hosted zone), or a certificate in ACM for the address and your
   own DNS.
4. If the GHCR packages are private: a GitHub token with `read:packages`, stored as a secret
   `{"username": "<github user>", "password": "<token>"}`; its ARN is
   `registry_credentials_secret_arn`.

## First deploy of an environment

```bash
cd infra/terraform
cp backend.hcl.example backend-staging.hcl   # bucket, key cctv/staging.tfstate
cp staging.tfvars.example staging.tfvars     # domain, zone, release tag, ...
terraform init -backend-config=backend-staging.hcl
terraform apply -var-file=staging.tfvars     # about 20 minutes (the database takes longest)
./deploy.sh staging v1.0.0                   # creates the tables
```

Then the first super-admin, from a shell in an API container (`terraform output exec_command`):

```bash
python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
python -m cctv_cloud.cli security-check --online
```

No email provider is set up yet, so the link to set the password is in the API's log
(`terraform output log_groups`, group `api`).

Browser notifications: `python -m cctv_cloud.cli vapid-keys` (anywhere), put the private key
in a Secrets Manager secret, add it to `secret_environment` and apply. The same for the
WhatsApp token and app secret and the on-call webhook URL.

## Each release

Tag the release (`git tag v1.2.0 && git push origin v1.2.0`; the release workflow builds the
images), then:

```bash
./deploy.sh staging v1.2.0      # migrations first, then the containers (asks before applying)
./deploy.sh production v1.2.0   # once staging looks right
```

The migrations always run with the new image before the containers change, and are written
so the previous version keeps working on the new schema. If they fail, nothing else changes.
To roll back, deploy the previous tag. Edge boxes are updated separately
([docs/runbooks/edge-recovery.md](../../docs/runbooks/edge-recovery.md#update-the-edge-software)).

## Checking the scripts

```bash
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
terraform test     # plans staging and production with mocked AWS (tests/plan.tftest.hcl)
```

CI runs these on every push.

## Good to know

- The state contains the generated passwords: only administrators read the state bucket.
- Changing `db_instance_class` or `redis_node_type` restarts the database or Redis in staging
  right away and in production in the maintenance window (Friday 03:00 Kuwait time).
- Destroying production needs deletion protection turned off first, on purpose.
- Client IPs reach the API through the load balancer (`X-Forwarded-For`, trusted because
  only the load balancer can reach the containers), for the audit log and sign-in limits.
