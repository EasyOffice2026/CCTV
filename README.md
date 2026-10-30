# AI CCTV Data Management System

Turns existing CCTV footage into searchable events, real-time alerts and daily reports.
Pilot site: Mudawara. Built as a multi-tenant product for later client rollout.

- Plan: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)
- Getting started: [docs/dev-setup.md](docs/dev-setup.md)
- Edge ↔ cloud API: [contracts/README.md](contracts/README.md)

## Repository layout

| Folder | Purpose |
|---|---|
| `contracts/` | Shared edge ↔ cloud API models (v1) and JSON Schemas |
| `edge/` | Edge AI service (Jetson): stream ingest, detection pipelines, rules, clip saving, cloud sync |
| `cloud/` | Cloud platform: FastAPI API (`cctv_cloud`), database models, Alembic migrations |
| `web/` | Web dashboard and admin panel (Next.js) |
| `ml/` | Dataset tools, labelling exports, training and evaluation scripts |
| `infra/` | docker-compose dev stack, simulated cameras, Postgres init, (later) Terraform |
| `docs/` | Plan, setup guide, onboarding checklist, runbooks |

## Data rules

- Do **not** commit camera footage, face images, model weights, NVR passwords or `.env` files.
- Client documents (briefs, site screenshots) stay outside this repository.
