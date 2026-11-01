# Architecture (plan 11.5)

For developers taking over the system. What runs where, how data moves, and where in the
code each part lives. The reasons behind the choices are in the plan
([DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) §1-§2); security in [security.md](security.md);
privacy in [privacy/](privacy/README.md).

## The parts

```
 Branch LAN                                           Cloud (one region)
 ┌────────────────────────────────────────┐          ┌──────────────────────────────────────────────┐
 │ Cameras ─RTSP─► NVR (24/7 recording)   │          │  nginx: web app (static) + /v1 proxy           │
 │    │                 ▲ ISAPI (playback) │          │      │                                         │
 │    └─RTSP──► Edge box (Jetson)          │  HTTPS   │  API (FastAPI, uvicorn)  ◄─► PostgreSQL (RLS)  │
 │              cctv_edge service ─────────┼────────► │      │    ▲                     Redis (pub/sub) │
 │              go2rtc (live, playback)    │ outbound │      ▼    │                                     │
 │              SQLite outbox, ring buffer │  only    │  Worker: alerts, webhooks, reports, retention, │
 │              NVMe (LUKS)                │          │          health check / on-call               │
 └────────────────────────────────────────┘          │  Object storage (S3): clips, snapshots          │
        ▲ WebRTC (DTLS-SRTP) live video ──────────────┼──► browser                                    │
                                                      │  Out: WhatsApp, Web Push, email, webhooks,     │
                                                      │       on-call, /metrics (Prometheus)           │
                                                      └──────────────────────────────────────────────┘
```

| Part | Code | Runs as |
|---|---|---|
| Contracts (edge ↔ cloud API models, JSON Schemas) | `contracts/` | Python package used by both sides |
| Edge service | `edge/cctv_edge/` | Docker on the Jetson (`infra/edge/`), one per branch |
| API | `cloud/cctv_cloud/api/`, `main.py` | Docker (`cloud/Dockerfile`), several replicas behind a load balancer (needs Redis for more than one) |
| Worker | `cloud/cctv_cloud/worker.py`, `services/` | Docker, the same image, `python -m cctv_cloud.worker run`, one replica |
| Database | `cloud/cctv_cloud/db/`, `cloud/migrations/` | PostgreSQL 15+ (managed) |
| Web app | `web/` | Static Next.js export served by nginx (`web/Dockerfile`) |
| ML tools | `ml/cctv_ml/` | On a workstation; not deployed |

## How an event travels

1. **Edge**: `streams.py` decodes the camera's sub-stream (5 fps) → `pipeline.py` runs the
   detectors the enabled features need (`detection.py`, `models/`) → `tracking.py`
   (ByteTrack) → zone tests → `rules.py` turns observations into open/closed events;
   `ppe.py`, the drawer classifier and `attendance.py` (faces, in memory only) add their
   observations or sightings; `counts.py` makes 5-minute count buckets.
2. `service.py` saves a snapshot, and later cuts a clip from the ring buffer
   (`ringbuffer.py`, `media.py`), into the **outbox** (`outbox.py`, SQLite), which survives
   restarts and internet outages.
3. `sync.py` uploads media to object storage through pre-signed URLs from the API
   (`POST /v1/edge/media/upload-urls`) and sends the event (`POST /v1/edge/events`),
   idempotent by `event_id`.
4. **API** (`api/edge.py`): checks every id against the box's branch, stores the event,
   routes it to alert rules (`services/alerts.py`) and webhooks (`services/webhooks.py`)
   **in the same transaction**, publishes it on Redis for live dashboards
   (`services/notify.py`, `/v1/ws`), and sends the alerts right after the commit. The
   worker retries what failed.
5. Reports (`services/reports.py`) and the overview and pilot figures are computed from the
   stored events, counts, attendance and alert deliveries.

The box also sends a **heartbeat** every 30 s (camera health, disk, temperature, backlog,
stream restarts). The cloud answers with the latest config version (the box then pulls
`GET /v1/edge/config`: cameras, zones, features already resolved camera → branch → tenant
→ default, so a switched-off feature never runs) and whether to rotate its token.

## Live view, playback, frames

The browser asks the API, which puts a request on the **relay** (`services/relay.py`, Redis
or in-process) for the branch's box; the box long-polls `GET /v1/edge/remote`, does the
work on the LAN (go2rtc WebRTC answer, NVR recordings search via ISAPI, a frame) and replies.
Video then flows box → browser over WebRTC; it never passes through the cloud.

## Tenancy and permissions

Every tenant table has `tenant_id` and a **row-level-security** policy; the API connects as
`cctv_app`, which can't bypass it, and sets the tenant per transaction
(`db/session.py: tenant_session`). Lookups that must happen before the tenant is known
(sign-in, device token, API key, SSO issuer) are SECURITY DEFINER functions in the
migrations. On top, `rbac.py` gives each role its permissions and branch access; every
endpoint declares what it needs (`api/deps.py: context(...)`). Tests sweep every route
across tenants and branches (`cloud/tests/test_security.py`).

## Background jobs (worker)

| Job | Every | Code |
|---|---|---|
| Alert retries, webhook retries | 5 s | `services/alerts.py`, `services/webhooks.py` |
| Offline boxes, health check, on-call | 1 min | `worker.py`, `services/monitoring.py` |
| Absences, scheduled reports | 5 min | `services/attendance.py`, `services/reports.py` |
| Retention (media, events, counts, health minutes, audit) | 1 h | `worker.py` |

## Where to change things

| To … | Change |
|---|---|
| Add an edge ↔ cloud field | `contracts/src/cctv_contracts/v1/` (additive), `python -m cctv_contracts.export`, then both sides |
| Add an event type or rule | `contracts` enums (`EventType`, `EVENT_FEATURE`), `edge/cctv_edge/rules.py` `DEFAULT_RULES`, labels in `web/src/lib/messages/` |
| Change the cloud servers (sizes, settings, secrets) | `infra/terraform` (`*.tfvars`, then `terraform apply` or `deploy.sh`) |
| Ship new edge software | tag `v*` (release workflow), then Administration › Edge software; the box side is `edge/cctv_edge/updater.py` |
| Add a model | a folder with `model.json` (edge/README.md "Models"); the task in `edge/cctv_edge/models/` |
| Add an endpoint | `cloud/cctv_cloud/api/`, then `python -m cctv_cloud.openapi` and `npm run api:types` in `web/` |
| Change the database | models in `cloud/cctv_cloud/db/models/`, `alembic revision --autogenerate`, add RLS for new tenant tables |
| Add a dashboard page | `web/src/app/(app)/…/page.tsx`, the menu in `web/src/components/AppShell.tsx`, text in both message files |
