# Developer setup

## Requirements

- Python 3.12+ (the edge targets 3.10; CI tests the contracts on both)
- Node.js 22+ (web dashboard)
- Docker Desktop (for the local stack and simulated cameras)
- Git

## 1. Python environment

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e "contracts[test]" -e "cloud[test]" -e "edge[test]" -e "ml[test]" pre-commit
pre-commit install
```

## 2. Local stack

```bash
docker compose -f infra/docker-compose.yml up -d
```

| Service | URL | Notes |
|---|---|---|
| Dashboard | http://localhost:3000 | Web app; proxies `/v1` to the API ([web/README.md](../web/README.md)) |
| API | http://localhost:8000/docs | Runs migrations on start ([cloud/README.md](../cloud/README.md)) |
| Worker | — | Alert retries, reports, retention, offline-device and absence jobs |
| Postgres | localhost:5432 | `cctv` (app) and `cctv_test` (tests) databases |
| MinIO | http://localhost:9001 | user `cctv`, password `cctv-dev-secret` |
| MQTT | localhost:1883 | anonymous (dev only) |
| Simulated cameras | rtsp://localhost:8554/… | see below |

Database roles:

- `cctv_owner`: owns the tables; used for migrations and system jobs.
- `cctv_app`: used by the API; **cannot bypass row-level security**, so it only sees the tenant
  set by `tenant_session(tenant_id)`.

First user: create a super-admin, open the printed link to set a password, then sign in
at http://localhost:3000 (2FA is set up at the first login). Create the first client under
Administration › Clients; its owner gets an invitation email (written to the API log until
an email provider is chosen).

```bash
docker compose -f infra/docker-compose.yml exec api python -m cctv_cloud.cli create-superadmin --email you@example.com --name "You"
```

## 3. Simulated cameras

Put exported footage (MP4, H.264/H.265) in `ml/data/footage/`. That folder is git-ignored and
must never be committed. Then open the stream in VLC or `ffplay`:

| File | RTSP URL |
|---|---|
| `ml/data/footage/ch1.mp4` | `rtsp://localhost:8554/Streaming/Channels/101` (main) and `…/102` (sub) |
| `ml/data/footage/kitchen.mp4` | `rtsp://localhost:8554/kitchen` |

The `Streaming/Channels/<ch>0<1|2>` form matches Hikvision NVRs, so the same edge config works
in development and on site.

## 4. Tests

```bash
pytest contracts
cd edge && pytest && cd ..   # edge service and site check; fake cloud, NVR and ffmpeg (edge/README.md)
cd ml && pytest && cd ..     # dataset split and evaluation tools (see ml/README.md)

# Cloud tests need Postgres (the docker stack above). The test database's schema is wiped.
export CCTV_TEST_MIGRATION_DATABASE_URL=postgresql+psycopg://cctv_owner:cctv_owner@localhost:5432/cctv_test
export CCTV_TEST_DATABASE_URL=postgresql+psycopg://cctv_app:cctv_app@localhost:5432/cctv_test
cd cloud && pytest
```

Web dashboard (`web/`):

```bash
cd web && npm ci && npm run lint && npm test && npm run build
```

Without those variables the database tests are skipped. What the cloud tests check:

- every table with `tenant_id` has a row-level-security policy, and the app role can't bypass it
- a tenant can't read, insert, update or link to another tenant's rows
- the audit log is append-only and the feature catalogue is read-only for the app
- face templates are refused without staff consent and deleted when staff leave
- RTSP URLs with embedded passwords are rejected
- models and migrations are in sync, and the migration downgrades and upgrades cleanly
- security (plan 7): every route needs credentials; no route with an id reaches another
  tenant's or another branch's rows; device token rotation; face template encryption (local
  and KMS); the audit log API, export and retention; 2FA lockout; security headers
- the API: login, 2FA, session rotation, password reset, every role's permissions, branch
  scoping, tenant isolation, API keys, edge pairing/config/ingest, search, audited media
  links, retention
- the live event WebSocket (branch filtering, token renewal, logout), live view / playback /
  frame / face enrolment through a simulated edge box (only the addressed box can answer),
  the relay on Redis (fakeredis) and in-process, staff consent rules, CSV export, the
  refresh cookie
- reports and keys: drawer openings without a sale alerted once and counted in reports with
  each camera's busiest areas (PDF and Excel); `rotate-secret-key` moving 2FA, sign-in
  integration and webhook secrets and face templates to a new key, harmless twice, nothing
  changed with a wrong key
- till and payroll: sales by API key, retries replacing receipts, drawer openings matched
  (cash first, each sale once) or marked without a sale only where the till is complete, the
  search filter; attendance per day and totals for payroll as JSON and CSV (formulas
  defused), owners and the payroll key only, audited
- objects left or removed: a bag put down or taken away in a watched area, someone walking
  past, lights switched on, a person standing still, long holds becoming normal; the
  event ends when the bag is taken away, without a false "removed"
- heatmaps: time per cell without staff and stream gaps, hourly buckets, merged after a
  restart in the outbox, stored and added up in the cloud per camera with branch access,
  through the real edge client
- edge software updates (plan 2.10): releases and rollouts for super-admins only, the
  offer in heartbeats, failed updates not offered again until rolled out again, withdrawn
  releases; on the box the updater's switch, health watch and rollback, refused requests
  (tag instead of digest, another repository, bad signature), with a stand-in for Docker;
  the whole round trip through the real edge client
- monitoring and onboarding (plan 11): incidents opened and resolved from heartbeats,
  on-call paged only for incidents that last, `/metrics` behind its token without tenant
  data, the onboarding checklist; `docs/api-reference.md` is up to date
- the pilot (plan 10): queue and staff figures in the overview and reports, the pilot
  figures against the success criteria (precision, false alarms, alert p95, uptime, day
  count), who may read and write the pilot log
- install and shadow mode (plan 9): health per minute from heartbeats (restart counting,
  other branches' cameras ignored), the setup test (pass, drops, missing minutes, running
  window, default start), shadow mode (alerts only to the support team, no webhooks, only
  super-admins switch it), review figures, the training export (audited, per tenant)
- the operational system (plan 8): sign-in tokens (replay, expiry, audience, keys,
  algorithms, users, owner 2FA), the overview and its feature switches, the `kpis:read`
  API key, webhooks (signature, retries, private addresses, reports)
- attendance from face sightings (work day, late/on time, overnight shifts, absences,
  corrections that survive sightings), face templates for the box (only consenting active
  staff of its branch), count buckets (upsert, branch scoping)

## 5. Database changes

```bash
cd cloud
alembic revision --autogenerate -m "describe the change"   # review the file before committing
alembic upgrade head
```

New tables with a `tenant_id` column need a row-level-security policy in their migration.
The `test_every_tenant_table_has_row_level_security` test fails until they have one.

After API changes, regenerate the OpenAPI file and the web app's types (CI checks both):

```bash
cd cloud && python -m cctv_cloud.openapi && cd ../web && npm run api:types   # also docs/api-reference.md
```

## 6. Contract changes

Edit `contracts/src/cctv_contracts/v1/`, then run `python -m cctv_contracts.export` and commit
the updated `contracts/schemas/v1/*.json`. Keep changes additive (see `contracts/README.md`).
