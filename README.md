# AI CCTV Data Management System

Turns existing CCTV footage into searchable events, real-time alerts and daily reports.
Pilot site: Mudawara. Built as a multi-tenant product for later client rollout.

- Plan: [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)
- Getting started: [docs/dev-setup.md](docs/dev-setup.md)
- Edge ↔ cloud API: [contracts/README.md](contracts/README.md)
- Edge service: [edge/README.md](edge/README.md)
- Cloud API: [cloud/README.md](cloud/README.md)
- Web dashboard: [web/README.md](web/README.md)
- Security (TLS, encryption, secrets, pen-test checklist): [docs/security.md](docs/security.md)
- Privacy (data flow, retention, consent form, signage): [docs/privacy/](docs/privacy/README.md)
- Connecting the till (POS) and payroll (HRM): [docs/integration/pos-and-payroll.md](docs/integration/pos-and-payroll.md)
- Architecture: [docs/architecture.md](docs/architecture.md); API reference: [docs/api-reference.md](docs/api-reference.md)
- Hardware kits and camera requirements: [docs/hardware.md](docs/hardware.md)
- Operations: [deploying the cloud](docs/runbooks/deploy-cloud.md), [onboarding a branch](docs/runbooks/onboarding.md), [edge recovery](docs/runbooks/edge-recovery.md), [retraining a model](docs/runbooks/retraining.md); handover: [docs/handover.md](docs/handover.md)
- Running the 30-day pilot and its sign-off form (EN/AR): [docs/pilot/](docs/pilot/README.md)
- Install day and shadow mode (runbooks): [docs/runbooks/](docs/runbooks/install.md); owner and manager guide (EN/AR): [docs/training/](docs/training/owner-and-managers.md)
- Connecting the client's operational system (sign-in, embedding, KPIs, webhooks): [docs/integration/ops-system.md](docs/integration/ops-system.md)

## Repository layout

| Folder | Purpose |
|---|---|
| `contracts/` | Shared edge ↔ cloud API models (v1) and JSON Schemas |
| `edge/` | Edge AI service (`cctv_edge`, Jetson): stream ingest, ring buffer, detection, rules, clips, offline outbox, cloud sync |
| `cloud/` | Cloud platform: FastAPI API (`cctv_cloud`), database models, Alembic migrations, worker ([README](cloud/README.md)) |
| `web/` | Web dashboard and admin panel (Next.js, English/Arabic) ([README](web/README.md)) |
| `ml/` | Dataset tools, labelling exports, training and evaluation scripts |
| `infra/` | docker-compose dev stack, simulated cameras, Postgres init, Jetson provisioning (`infra/edge/`), AWS cloud (`infra/terraform/`) |
| `docs/` | Plan, setup guide, onboarding checklist, runbooks |

## Data rules

- Do **not** commit camera footage, face images, model weights, NVR passwords or `.env` files.
  CI and the pre-commit hook scan for secrets (gitleaks, `.gitleaks.toml`).
- Client documents (briefs, site screenshots) stay outside this repository.
