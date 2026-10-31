# cctv-cloud: multi-tenant cloud API

FastAPI service for the web dashboard, integrations and edge devices. Interactive API docs
are at `/docs` when it runs.

## Tenant isolation

Every request runs in one database transaction with the caller's tenant set, and
PostgreSQL row-level security (RLS) hides all other tenants' rows. The API connects as
`cctv_app`, which cannot bypass RLS. Permission and branch checks in the code come on top
of that.

A few things must work before the tenant is known: login, device and API-key
authentication, and pairing. These use narrow database functions (`auth_find_user`,
`auth_find_device`, `auth_find_api_key`, `auth_find_pairing`) that return only ids. See
migration `0002`.

## Signing in

| Who | How |
|---|---|
| Users | `POST /v1/auth/login` → access token (15 min) + refresh token. Send `Authorization: Bearer <access>` |
| Owners, super-admins | Must use 2FA (TOTP): login returns an `mfa_token`, then `/mfa/verify` (or `/mfa/setup` + `/mfa/enable` the first time) |
| Super-admins | Pick the tenant to act on with the `X-Tenant-Id` header |
| Integrations | `X-API-Key: cctv_…`, created under `/v1/api-keys` with scopes (`events:read`) |
| Edge devices | `/v1/edge/*` with the device token from pairing (see `contracts/README.md`) |

Sessions end after 8 hours without use and after 7 days in any case. Each refresh replaces
the refresh token; reusing an old one ends the session (stolen-token protection).
Changing or resetting the password signs out all other sessions. Accounts lock for 15
minutes after 5 wrong passwords.

## Roles

| Permission | Owner | Branch manager | Reviewer | Installer |
|---|:-:|:-:|:-:|:-:|
| Users, tenant settings, branches, API keys, audit log | ✓ | | | |
| Working hours | ✓ | ✓ | | |
| Feature on/off and thresholds (branch, camera) | ✓ | ✓ | | |
| Feature on/off for the whole tenant | ✓ | | | |
| NVRs, cameras, zones, edge devices | ✓ | | | ✓ |
| Search and view events | ✓ | ✓ | ✓ | |
| Review events (valid / false alarm) | ✓ | ✓ | ✓ | |
| View snapshots and clips | ✓ | ✓ | ✓ | |
| Download snapshots and clips | ✓ | ✓ | | |

Owners see every branch. Other roles see only the branches assigned to them; anything
in other branches returns 404. Super-admins (our team) can do everything, in any tenant.

## Endpoints

| Area | Paths |
|---|---|
| Auth | `/v1/auth/login`, `/mfa/*`, `/refresh`, `/logout`, `/me`, `/password/*` |
| Platform (super-admin) | `/v1/platform/tenants` |
| Organisation | `/v1/tenant`, `/v1/branches`, `/v1/branches/{id}/working-hours`, `/v1/users` |
| Devices | `/v1/nvrs`, `/v1/cameras`, `/v1/cameras/{id}/zones`, `/v1/zones/{id}`, `/v1/edge-devices` |
| Features | `/v1/features`, `/v1/feature-settings`, `/v1/cameras/{id}/features` (resolved) |
| Events | `/v1/events` (search), `/v1/events/{id}`, `/review`, `/media/{snapshot\|clip}` |
| Integrations | `/v1/api-keys`, `/v1/audit-logs` |
| Edge | `/v1/edge/pair`, `/config`, `/heartbeat`, `/media/upload-urls`, `/events` |

**Feature settings** resolve camera → branch → tenant → default; `params` merge in the same
order. Any change that affects a branch's edge config bumps the branch's
`config_version` and publishes on Redis `config:<branch_id>`. The edge also sees the new
version in its next heartbeat reply, at most 30 s later.

**Event ingest** is idempotent by `event_id`. Each event is accepted or rejected on its
own. Every camera, zone, staff member and media key in an event is checked against the
device's branch. A late "open" retry never reopens a closed event. Accepted events are
published on Redis `events:<tenant_id>` (live dashboard and alerts, Phases 4 and 6).

**Media** links are pre-signed, last 5 minutes, and every view or download is written to
the audit log in the same transaction.

## Operations

```bash
python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
python -m cctv_cloud.worker run          # retention + offline devices, every 5 min
```

The **retention** job deletes clips and snapshots older than the tenant's
`retention_media_days` (default 90), then event records older than
`retention_event_days` (default 365). Configure an object-storage lifecycle rule as a
backstop; the dev stack sets 400 days.

**Email** (password reset, invitations) is written to the log until a provider is chosen.
