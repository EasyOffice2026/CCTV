# Deploying and running the cloud (plan 11.5)

For the engineer who runs production. The infrastructure-as-code (Terraform, plan 1.4)
isn't written yet; this is what it has to create, and how to deploy and operate the
system on it. Security requirements are in [../security.md](../security.md).

## What has to exist

| Piece | AWS (region: plan §1 #11, e.g. me-central-1) | Notes |
|---|---|---|
| PostgreSQL 15+ | RDS, Multi-AZ, **storage encryption on** (can't be added later), automated backups 14+ days | Two roles: `cctv_owner` (owns tables, migrations, worker's system jobs) and `cctv_app` (API; can't bypass row-level security). See `infra/postgres/init` |
| Object storage | S3 bucket, **default encryption** (SSE-S3 or SSE-KMS), public access blocked, lifecycle rule deleting objects after 400 days as a backstop | CORS: GET from the dashboard origin |
| Redis | ElastiCache, TLS (`rediss://`) | Live events and the relay between API replicas |
| Containers | ECS Fargate or EKS: **api** (2+ replicas), **worker** (exactly 1), **web** (2) | Images from the release workflow (below) |
| Load balancer | ALB with TLS (ACM certificate) for the dashboard and API on one origin; WebSocket allowed; idle timeout ≥ 75 s (edge long polls) | Only nginx (web) is exposed; it proxies `/v1` to the API |
| Keys | KMS key for face templates (`CCTV_FACE_KEY=aws-kms:…`), the API role allowed `kms:GenerateDataKey` and `kms:Decrypt` | |
| Secrets | Secrets Manager, mounted as files in `CCTV_SECRETS_DIR` (one file per setting, e.g. `cctv_secret_key`) | Never in images or task definitions in clear |
| Email | An SMTP or API provider (not chosen yet; until then emails go to the log) | |
| Monitoring | Prometheus (or CloudWatch agent) scraping `GET /metrics` with `CCTV_METRICS_TOKEN`; logs to CloudWatch; on-call email and/or chat webhook | |

## Settings

All `CCTV_*`; the full list with defaults is `cloud/cctv_cloud/config.py`. In production at
least:

| Setting | Value |
|---|---|
| `CCTV_ENVIRONMENT` | `production` (refuses dev secrets, plain-http URLs, a missing face key) |
| `CCTV_DATABASE_URL`, `CCTV_MIGRATION_DATABASE_URL` | `postgresql+psycopg://cctv_app:…@…/cctv?sslmode=verify-full`, and the owner role |
| `CCTV_SECRET_KEY` | 32+ random characters (signs sessions; encrypts TOTP, SSO and webhook secrets) |
| `CCTV_WEB_URL` | `https://cctv.<domain>` (links in emails and messages; the SSO audience) |
| `CCTV_S3_BUCKET`, `CCTV_S3_REGION`, `CCTV_S3_ENDPOINT_URL` (empty on AWS), keys or the task role | |
| `CCTV_REDIS_URL` | `rediss://…` |
| `CCTV_FACE_KEY` | `aws-kms:alias/cctv-face` |
| `CCTV_WHATSAPP_*`, `CCTV_VAPID_PRIVATE_KEY` | from Meta and `python -m cctv_cloud.cli vapid-keys` |
| `CCTV_ONCALL_EMAILS`, `CCTV_ONCALL_WEBHOOK_URL` | who is told about offline boxes and dropped streams |
| `CCTV_METRICS_TOKEN` | 24+ random characters, also in the Prometheus scrape config |

The web container takes `API_UPSTREAM`, `MEDIA_ORIGIN` (the S3 bucket's origin, for the
CSP) and `EMBED_ANCESTORS` (the operational system's origins, plan 8).

## Release and deploy

1. Merge to `main` with CI green (lint, secrets scan, all test suites, image builds).
2. Tag a release: `git tag v1.2.0 && git push origin v1.2.0`. The release workflow
   (`.github/workflows/release.yml`) pushes `cctv-api`, `cctv-web` and the multi-arch
   `cctv-edge` images to GitHub Container Registry with that tag.
3. **Migrations first**, as the owner role, from the new image:
   `alembic upgrade head` (in `/srv/cloud`, the image's working directory). Migrations are written to work with
   the previous API version still running (add columns, never rename in one step).
4. Roll the API, then the worker, then web, to the new tag. The API's container health
   check is `/healthz`; the load balancer should use `/readyz` (checks the database).
5. `python -m cctv_cloud.cli security-check --online` in the new API container: no errors.
6. Boxes are updated separately, after the cloud: add the edge release under
   Administration › Edge software and roll it out to one box first
   ([edge-recovery.md](edge-recovery.md#update-the-edge-software)). The cloud must be
   deployed first: a newer box talks to an older cloud, never the other way round.

Rolling back: deploy the previous tag. Roll a migration back only if the release notes say
it is safe (`alembic downgrade <revision>`); otherwise fix forward.

## Every day / week

- System health page (super-admins): open incidents of every client.
- Prometheus alerts worth having on top of on-call: `cctv_up` missing, `cctv_alert_deliveries_queued`
  or `cctv_webhook_deliveries_queued` above 100 for 10 minutes (the worker is stuck or a
  channel is down), `cctv_edge_devices{status="offline"}` rising.
- Weekly: RDS backups and a restore test once a quarter; `pip-audit` and `npm audit`;
  dependency updates through a normal release.

## First start of a new environment

```bash
alembic upgrade head                                             # as the owner role
python -m cctv_cloud.cli create-superadmin --email you@example.com --name "You"
python -m cctv_cloud.cli security-check --online
```

Then sign in, set up 2FA, and create the first client under Administration › Clients.
Onboarding a branch: [onboarding.md](onboarding.md).
