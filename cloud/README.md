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
| Users | `POST /v1/auth/login` → access token (15 min) + refresh token. Send `Authorization: Bearer <access>`. Browsers also get the refresh token as an HttpOnly, SameSite=Strict cookie (path `/v1/auth`); `/refresh` accepts it instead of a body |
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
| Download snapshots and clips, export event lists (CSV) | ✓ | ✓ | | |
| Live view and NVR playback | ✓ | ✓ | | |
| Current camera frame (for drawing zones) | ✓ | ✓ | | ✓ |
| Staff, shifts, consent, face enrolment, attendance (view and correct) | ✓ | ✓ | | |

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
| Events | `/v1/events` (search), `/v1/events/export` (CSV), `/v1/events/{id}`, `/review`, `/media/{snapshot\|clip}` |
| Live view, playback | `/v1/live/ice-servers`, `/v1/cameras/{id}/live`, `/playback`, `/recordings`, `/frame` |
| Staff | `/v1/staff`, `/v1/staff/{id}/consent`, `/v1/staff/{id}/face`, `/v1/shifts` |
| Attendance, counts | `/v1/attendance`, `/v1/attendance/{staff_id}/{date}` (correct / undo), `/v1/counts` |
| Realtime | WebSocket `/v1/ws` (live event feed) |
| Integrations | `/v1/api-keys`, `/v1/audit-logs` |
| Edge | `/v1/edge/pair`, `/config`, `/heartbeat`, `/media/upload-urls`, `/events`, `/remote` (long poll), `/remote/{id}/reply`, `/face-templates`, `/attendance`, `/counts` |

**Feature settings** resolve camera → branch → tenant → default; `params` merge in the same
order. Any change that affects a branch's edge config bumps the branch's
`config_version` and publishes on Redis `config:<branch_id>`. The edge also sees the new
version in its next heartbeat reply, at most 30 s later.

**Event ingest** is idempotent by `event_id`. Each event is accepted or rejected on its
own. Every camera, zone, staff member and media key in an event is checked against the
device's branch. A late "open" retry never reopens a closed event. Accepted events are
published on Redis `events:<tenant_id>` (live dashboard and alerts, Phases 4 and 6).

**Media** links are pre-signed, last 5 minutes, and every view or download is written to
the audit log in the same transaction. The CSV export is audited with the filters used,
and cells that would start a spreadsheet formula are escaped.

**Live event feed** (`/v1/ws`): the client sends `{"type": "auth", "token": ...}` first
(tokens never go in URLs), then gets `{"type": "event", ...}` for events in its branches,
read from Redis `events:<tenant_id>`. The session is re-checked every minute; the socket
closes with code 4401 when the token expires without renewal or the session ends.

**Live view, playback, frames and face enrolment go through the edge box**, because only the
box can reach the cameras and NVR on the branch LAN and the box is behind NAT. The API puts
a request on the box's queue (Redis `relay:q:<device>`), the box long-polls
`GET /v1/edge/remote`, and answers with `POST /v1/edge/remote/{id}/reply`; only the box
the request was sent to can answer, and only once (an atomic Redis script). The waiting
browser request gets the answer, or 503 if the branch has no online box, 504 if the box
doesn't answer in 15 s, 502 with the box's message if it fails. For video only the WebRTC
offer/answer passes through the API; the stream goes from the box to the browser directly
or through a TURN server (`CCTV_WEBRTC_ICE_SERVERS`). Every view is audited
(`view_live`, `view_playback`, `view_frame`) before the box is asked.

**Face enrolment** requires recorded consent (also a database trigger). The photos are
relayed to the box, embedded by its face model, and dropped; only the embedding is stored,
encrypted (`app-secret:v1` key, to move to KMS in plan 7.2). Withdrawing consent or marking
someone as left deletes their face data (database trigger). A box without a face model
answers "face model not installed" and enrolment returns 502.

**Attendance** (plan 5.4). The box downloads the templates of its branch's active,
consenting staff (`GET /v1/edge/face-templates`, decrypted, kept in memory on the box only)
and sends only matches (`POST /v1/edge/attendance`: staff, camera, time). Sightings are
not stored: each one updates the person's work-day record (`attendance_records`): first
sighting = check-in, last = check-out, on time or late against the shift start plus grace.
Sightings until 4 h after the end of a shift that runs past midnight count for the day it
started. A manager's correction (`PUT /v1/attendance/{staff_id}/{date}`, audited) is kept
next to the detected times and survives later sightings; `DELETE` goes back to the detected
times. The worker marks staff **absent** once their shift is over if they have a shift and
an enrolled face but weren't seen, on days the branch is open (all days when no working
hours are set). Changes to who can be recognised (enrolment, consent, leaving, moving
branch) bump the branch config version so the box reloads the templates.

**Counts** (`POST /v1/edge/counts`): 5-minute buckets per camera and zone: entrance-line
in/out and zone occupancy (`max_occupancy`, `occupied_seconds`, e.g. how long the counter
was staffed). A bucket sent again replaces the stored one. Read with `GET /v1/counts`.

Without Redis (`CCTV_REDIS_URL=` empty) an in-process bus and relay are used, which only
work with a single API process.

## Operations

```bash
python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
python -m cctv_cloud.worker run          # retention, offline devices, absences; every 5 min
```

The **retention** job deletes clips and snapshots older than the tenant's
`retention_media_days` (default 90), then event records older than
`retention_event_days` (default 365), together with count buckets and attendance days. Configure an object-storage lifecycle rule as a
backstop; the dev stack sets 400 days.

**Email** (password reset, invitations) is written to the log until a provider is chosen.
