# AI CCTV Data Management System

Turns existing CCTV footage into searchable events, real-time alerts and daily reports.
Pilot site: Mudawara. Built as a multi-tenant product for later client rollout.

See [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) for the full plan.

## Repository layout

| Folder | Purpose |
|---|---|
| `edge/` | Edge AI service (Jetson): stream ingest, detection pipelines, rules, clip saving, cloud sync |
| `cloud/` | Cloud platform: FastAPI API, background workers, database migrations |
| `web/` | Web dashboard and admin panel (Next.js) |
| `ml/` | Dataset tools, labelling exports, training and evaluation scripts |
| `infra/` | Terraform, docker-compose, edge provisioning |
| `docs/` | Plan, API docs, onboarding checklist, runbooks |

## Data rules

- Do **not** commit camera footage, face images, model weights, NVR passwords or `.env` files.
- Client documents (briefs, site screenshots) stay outside this repository.
