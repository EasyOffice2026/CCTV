# Developer setup

## Requirements

- Python 3.12+ (the edge targets 3.10; CI tests the contracts on both)
- Docker Desktop (for the local stack and simulated cameras)
- Git

## 1. Python environment

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e "contracts[test]" -e "cloud[test]" pre-commit
pre-commit install
```

## 2. Local stack

```bash
docker compose -f infra/docker-compose.yml up -d
```

| Service | URL | Notes |
|---|---|---|
| API | http://localhost:8000/docs | Runs migrations on start |
| Postgres | localhost:5432 | `cctv` (app) and `cctv_test` (tests) databases |
| MinIO | http://localhost:9001 | user `cctv`, password `cctv-dev-secret` |
| MQTT | localhost:1883 | anonymous (dev only) |
| Simulated cameras | rtsp://localhost:8554/… | see below |

Database roles:

- `cctv_owner`: owns the tables; used for migrations and system jobs.
- `cctv_app`: used by the API; **cannot bypass row-level security**, so it only sees the tenant
  set by `tenant_session(tenant_id)`.

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

# Cloud tests need Postgres (the docker stack above). The test database's schema is wiped.
export CCTV_TEST_MIGRATION_DATABASE_URL=postgresql+psycopg://cctv_owner:cctv_owner@localhost:5432/cctv_test
export CCTV_TEST_DATABASE_URL=postgresql+psycopg://cctv_app:cctv_app@localhost:5432/cctv_test
cd cloud && pytest
```

Without those variables the database tests are skipped. What the cloud tests check:

- every table with `tenant_id` has a row-level-security policy, and the app role can't bypass it
- a tenant can't read, insert, update or link to another tenant's rows
- the audit log is append-only and the feature catalogue is read-only for the app
- face templates are refused without staff consent and deleted when staff leave
- RTSP URLs with embedded passwords are rejected
- models and migrations are in sync, and the migration downgrades and upgrades cleanly

## 5. Database changes

```bash
cd cloud
alembic revision --autogenerate -m "describe the change"   # review the file before committing
alembic upgrade head
```

New tables with a `tenant_id` column need a row-level-security policy in their migration.
The `test_every_tenant_table_has_row_level_security` test fails until they have one.

## 6. Contract changes

Edit `contracts/src/cctv_contracts/v1/`, then run `python -m cctv_contracts.export` and commit
the updated `contracts/schemas/v1/*.json`. Keep changes additive (see `contracts/README.md`).
