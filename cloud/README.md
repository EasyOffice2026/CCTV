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

Security and privacy controls (TLS, encryption, secrets, audit log, consent, the pen-test
checklist) are described in [docs/security.md](../docs/security.md) and
[docs/privacy/](../docs/privacy/README.md).

## Signing in

| Who | How |
|---|---|
| Users | `POST /v1/auth/login` → access token (15 min) + refresh token. Send `Authorization: Bearer <access>`. Browsers also get the refresh token as an HttpOnly, SameSite=Strict cookie (path `/v1/auth`); `/refresh` accepts it instead of a body |
| Owners, super-admins | Must use 2FA (TOTP): login returns an `mfa_token`, then `/mfa/verify` (or `/mfa/setup` + `/mfa/enable` the first time) |
| Super-admins | Pick the tenant to act on with the `X-Tenant-Id` header |
| Integrations | `X-API-Key: cctv_…`, created under `/v1/api-keys` with scopes (`events:read`, `kpis:read`, `attendance:read` for payroll, `pos:write` for the till) |
| The client's operational system | `POST /v1/auth/sso` with a short-lived JWT its server signed for the user (plan 8; `docs/integration/ops-system.md`). Gives a normal session of the invited user with that email; `embedded: true` (in an iframe) sets no cookie, the refresh token goes in the body |
| Edge devices | `/v1/edge/*` with the device token from pairing (see `contracts/README.md`) |

Sessions end after 8 hours without use and after 7 days in any case. Each refresh replaces
the refresh token; reusing an old one ends the session (stolen-token protection).
Changing or resetting the password signs out all other sessions. Accounts lock for 15
minutes after 5 wrong passwords or 2FA codes in a row (the count only resets after a full
sign-in, so a known password doesn't give more guesses at the code). Sign-ins and failed
sign-ins are in the audit log; a super-admin's go to the platform audit log.

## Roles

| Permission | Owner | Branch manager | Reviewer | Installer |
|---|:-:|:-:|:-:|:-:|
| Users, tenant settings, branches, API keys, audit log (view, export) | ✓ | | | |
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
| Branch reports (view, make, download) | ✓ | ✓ | | |
| Overview: branch totals (`kpis_view`) | ✓ | ✓ | | |
| Integrations: API keys, webhooks (sign-in integrations: our team) | ✓ | | | |
| Own alert rules (events of their branches) | ✓ | ✓ | ✓ | |

Owners see every branch. Other roles see only the branches assigned to them; anything
in other branches returns 404. Super-admins (our team) can do everything, in any tenant.

## Endpoints

| Area | Paths |
|---|---|
| Auth | `/v1/auth/login`, `/mfa/*`, `/refresh`, `/logout`, `/me`, `/password/*` |
| Platform (super-admin) | `/v1/platform/tenants`, `/v1/platform/audit-logs` (super-admin sign-ins) |
| Organisation | `/v1/tenant`, `/v1/branches`, `/v1/branches/{id}/working-hours`, `/v1/users` |
| Devices | `/v1/nvrs`, `/v1/cameras`, `/v1/cameras/{id}/zones`, `/v1/zones/{id}`, `/v1/edge-devices` |
| Features | `/v1/features`, `/v1/feature-settings`, `/v1/cameras/{id}/features` (resolved) |
| Events | `/v1/events` (search), `/v1/events/export` (CSV), `/v1/events/{id}`, `/review`, `/media/{snapshot\|clip}` |
| Live view, playback | `/v1/live/ice-servers`, `/v1/cameras/{id}/live`, `/playback`, `/recordings`, `/frame` |
| Staff | `/v1/staff`, `/v1/staff/{id}/consent`, `/v1/staff/{id}/face`, `/v1/shifts` |
| Attendance, counts | `/v1/attendance`, `/v1/attendance/{staff_id}/{date}` (correct / undo), `/v1/counts` |
| Till and payroll | `POST /v1/pos/sales` (`pos:write`), `GET /v1/payroll/attendance` (owners, `attendance:read`; JSON or CSV); `pos_match` on events and as a search filter. Guide: `docs/integration/pos-and-payroll.md` |
| Heatmaps | `/v1/heatmaps?camera_id=&from=&to=` (the camera's grid added up over the period, up to 93 days; `null` without data) |
| Alerts | `/v1/me/alert-rules`, `/v1/me/alert-profile`, `/v1/me/push-subscriptions`, `/v1/me/alert-test`, `/v1/events/{id}/alerts` (who was told, how fast) |
| Reports | `/v1/reports` (list, make), `/v1/reports/{id}`, `/v1/reports/{id}/file?format=pdf\|xlsx&lang=en\|ar` |
| Realtime | WebSocket `/v1/ws` (live event feed, in-app alerts) |
| Webhooks | `/v1/webhooks/whatsapp` (delivery reports from Meta; signed) |
| Edge software (plan 2.10) | `/v1/platform/edge-releases` (list, add), `.../{version}/rollout`, `.../{version}/withdraw`, `/v1/platform/edge-devices` (every client's boxes and their update state); the offer itself goes out with `POST /v1/edge/heartbeat` |
| Monitoring (plan 11.4) | `/v1/incidents` (devices managers), `/v1/platform/incidents` (super-admin), `/metrics` (Prometheus, `CCTV_METRICS_TOKEN`) |
| Onboarding (plan 11.1) | `/v1/branches/{id}/onboarding` |
| Pilot (plan 10) | `/v1/pilot/kpis` (figures against the success criteria), `/v1/pilot/log` (+ `/{id}`), `/v1/branches/{id}/pilot` (day 1, super-admin) |
| Install, shadow mode (plan 9) | `/v1/branches/{id}/stream-test` (48-hour setup test), `/v1/branches/{id}/shadow` (super-admin), `/v1/review-summary` (review results for tuning) |
| Overview, KPIs | `/v1/overview` (per branch, today so far or a past day; follows the feature switches) |
| Integrations | `/v1/api-keys`, `/v1/webhooks` (+ `/{id}/test`, `/{id}/deliveries`), `/v1/sso-integrations` (super-admin creates; owners see) |
| Audit log | `/v1/audit-logs` (filters: action, user, target, since/until), `/v1/audit-logs/export` (CSV, itself audited) |
| Edge | `/v1/edge/pair`, `/token` (rotation), `/config`, `/heartbeat`, `/media/upload-urls`, `/events`, `/remote` (long poll), `/remote/{id}/reply`, `/face-templates`, `/attendance`, `/counts`, `/heatmaps` |

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

**Till and cash drawer** (`services/pos.py`). Sales are stored by receipt (`pos_sales`, kept
as long as events) and each branch keeps `pos_synced_until`, what its till is complete up to.
Every minute the worker (`worker pos` once) matches each unchecked `drawer_open` event that is
at least 2 minutes before that time to a sale from 1 minute before to 2 after (cash first,
nearest; one sale per opening) and sets `pos_match` (`sale` with `pos_receipt_id`, or
`no_sale`, raised to warning). These are columns, not `attributes`, so the edge's updates to
the event don't overwrite them.

**Heatmaps** (`POST /v1/edge/heatmaps`): one bucket per camera and hour, person-seconds per
cell of a grid over the picture as `[cell index, seconds]` pairs (`heatmap_buckets`, kept
as long as event records). `GET /v1/heatmaps` adds a camera's buckets up over a period;
if the grid size changed within it, the most common size is used.

**Alerts** (plan 6.1-6.5, 6.7). Each user keeps their own alert rules: feature (or all),
branch (or all of theirs), minimum severity, channels (browser, WhatsApp, email), quiet
hours (branch time; only critical alerts get through) and whether to say when the event
ends. When the edge reports a new event, the API matches the rules in the same transaction
and queues one `alert_deliveries` row per user and channel: unique per event, user, channel
and kind, so retries and updates never alert twice. It also checks again that the user is
active, may see events and has the branch, and skips a user and channel already alerted for
the same camera, zone and event type within `CCTV_ALERT_COOLDOWN_SECONDS` (300). Events that
arrive more than `CCTV_ALERT_MAX_AGE_MINUTES` (30) after they started (e.g. buffered during an
outage) don't alert. After the commit the API sends right away; the worker retries what is
still queued (backoff 5 s to 5 min, at most 5 attempts) and drops messages older than an
hour. `latency_ms` is event start (edge clock) to the channel accepting the message, and
`GET /v1/events/{id}/alerts` and the reports show it.

* **Browser**: an in-app toast over the WebSocket (`{"type": "alert", ...}`, to that user
  only) and Web Push to the user's registered browsers (RFC 8291 encryption and VAPID,
  `services/webpush.py`; only Google, Mozilla, Microsoft and Apple push services are
  contacted). Generate the key with `python -m cctv_cloud.cli vapid-keys` and set
  `CCTV_VAPID_PRIVATE_KEY` and `CCTV_VAPID_SUBJECT` (a `mailto:`). Without it, alerts show
  only while a dashboard tab is open.
* **WhatsApp** (Cloud API): only to users who entered a number and agreed (the consent
  time is stored, and reset when the number changes); at most `CCTV_WHATSAPP_MAX_PER_HOUR`
  (20) per user. No images are sent, only a link that needs a sign-in. Set
  `CCTV_WHATSAPP_PHONE_NUMBER_ID` and `CCTV_WHATSAPP_TOKEN` (a system-user token); without
  them messages go to the log. Delivery reports: subscribe the app's webhook to
  `https://<api>/v1/webhooks/whatsapp` with `CCTV_WHATSAPP_VERIFY_TOKEN`, and set
  `CCTV_WHATSAPP_APP_SECRET` to check the signatures; "delivered" sets `delivered_at`.
* **Email**: through the mailer (the log until a provider is chosen).

WhatsApp templates to submit to Meta (names in `CCTV_WHATSAPP_TEMPLATES`), in English
(`en`) and Arabic (`ar`), category Utility, each with one URL button whose dynamic suffix is
the id:

| Template | Body (en) | Button URL |
|---|---|---|
| `cctv_alert` | `{{1}} at {{2}}, {{3}}, {{4}}.` (what, branch, camera and zone, time) | `https://<web>/events/?event={{1}}` |
| `cctv_alert_resolved` | `Ended: {{1}} at {{2}}, {{3}}, {{4}}.` | `https://<web>/events/?event={{1}}` |
| `cctv_report` | `{{1}} for {{2}} ({{3}}) is ready.` (report, branch, period) | `https://<web>/reports/?report={{1}}` |

**Reports** (plan 6.6). A report summarises one branch for a day, week (from Sunday,
`CCTV_REPORT_WEEK_START`) or month: events by type and review result, critical and security
events, compliance issues, cashier events, attendance (per person: on time, late, absent,
attendance and punctuality rates, compliance issues matched to them), customers in from
entrance lines by hour (peak hour), dining-zone peak occupancy, how long the counter was
staffed, and alert times (p50, p95, share within 10 s, failures, time to "delivered").
False alarms are counted but not as issues. The figures are stored as JSON
(`reports.data`); PDF (ReportLab, Arabic shaped with arabic-reshaper and python-bidi) and
Excel files are rendered on download in the reader's language, and downloads are audited
(`export_report`). The worker makes yesterday's report after `CCTV_REPORT_HOUR` (2:00 branch
time, so overnight events buffered by the box are in), the weekly and monthly ones after
the period, catches up three days back, and sends a link by email or WhatsApp to users who
asked for one (Alerts page) and may see the branch. Managers can make or remake any period,
including the current one ("so far"). Reports are kept `CCTV_REPORT_RETENTION_DAYS` (730).
PDFs need a font with Arabic letters: the API image installs DejaVu Sans; elsewhere set
`CCTV_REPORT_FONT` if none of the usual fonts is found.

Without Redis (`CCTV_REDIS_URL=` empty) an in-process bus and relay are used, which only
work with a single API process.

## Operations

```bash
python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
python -m cctv_cloud.cli vapid-keys      # key for browser notifications
python -m cctv_cloud.cli face-key        # a local key for face templates (when not using KMS)
python -m cctv_cloud.cli rotate-face-keys  # re-encrypt templates after changing CCTV_FACE_KEY
python -m cctv_cloud.cli security-check --online  # settings, TLS, keys, bucket encryption
python -m cctv_cloud.cli sso-token --issuer <iss> --email <user>  # test sign-in link (HS256)
python -m cctv_cloud.cli export-reviewed --tenant <slug> --since 2026-11-15 --out DIR
                                         # reviewed events + media for retraining (audited)
python -m cctv_cloud.worker health       # once: offline boxes, incidents, on-call
python -m cctv_cloud.worker run          # alert and webhook retries every 5 s; offline boxes and
                                         # the health check every minute; offline devices,
                                         # absences and reports every 5 min; retention hourly
```

The **retention** job deletes clips and snapshots older than the tenant's
`retention_media_days` (default 90), then event records older than
`retention_event_days` (default 365), together with count buckets and attendance days. Configure an object-storage lifecycle rule as a
backstop; the dev stack sets 400 days. Audit entries older than
`CCTV_AUDIT_RETENTION_DAYS` (730) are removed through the `audit_purge` database function,
which never deletes anything younger than a year; the app role can't delete them itself.

**Secrets** come from environment variables or, better, from files: set `CCTV_SECRETS_DIR`
to a mounted secrets folder (Docker or Kubernetes secrets) holding one file per setting,
named like the variable in lower case (`cctv_secret_key`, `cctv_s3_secret_key`,
`cctv_whatsapp_token`, ...). In staging and production the API refuses to start with the
dev secret key or with plain-http `CCTV_WEB_URL` / `CCTV_S3_ENDPOINT_URL`, and in
production without `CCTV_FACE_KEY`.

**Face templates** are encrypted with AES-256-GCM, bound to the tenant and staff member,
under the key named by `CCTV_FACE_KEY`: `aws-kms:<key ARN or alias>` (a KMS data key per
template; the API role needs `kms:GenerateDataKey` and `kms:Decrypt` on that key, with the
encryption context `purpose=cctv-face-template`) or `local:<id>` with the key in
`CCTV_FACE_KEYS`. To rotate: add the new key, point `CCTV_FACE_KEY` at it, run
`rotate-face-keys`, then remove the old key.

**Device tokens** are rotated every `CCTV_DEVICE_TOKEN_DAYS` (30): the heartbeat reply says
`rotate_token`, the box calls `POST /v1/edge/token`, saves the new token and uses it. The
old one keeps working for `CCTV_DEVICE_TOKEN_GRACE_MINUTES` (10). Pairing again or retiring
the box ends all its tokens.

**Edge software updates** (plan 2.10). `edge_releases` is a catalogue shared by all clients (no row-level security, like `features`); a box's `target_version` is set by a rollout. While a box runs another version, every heartbeat response carries the release (version, image by digest, and the rollout time, which identifies the request). The box's report comes back in the heartbeat (`update_*` columns); a failed or rolled-back update for the same rollout isn't offered again, and rolling out again retries it. Withdrawing a release stops offering it. Rollouts are in each client's audit log (`software_update`).

**Monitoring** (plan 11.4). Heartbeats store the box's disk, temperature, CPU/GPU and upload backlog. Every minute the worker marks silent boxes offline and the health check (`services/monitoring.py`) opens or resolves `health_incidents`: box offline, a camera without frames for `CCTV_MONITOR_STREAM_DOWN_MINUTES` (5, only while its box is online), disk under `CCTV_MONITOR_DISK_FREE_GB` (20), temperature at `CCTV_MONITOR_TEMPERATURE_C` (85), backlog at `CCTV_MONITOR_SYNC_BACKLOG` (1,000). On-call (`CCTV_ONCALL_EMAILS`, `CCTV_ONCALL_WEBHOOK_URL` taking `{"text": ...}`) is told once an incident has lasted `CCTV_MONITOR_NOTIFY_AFTER_MINUTES` (5) and when it is resolved; shorter ones are closed silently. `GET /metrics` gives counts across tenants (boxes, cameras, open incidents, queued deliveries) through the `platform_monitoring()` database function. The API reference is generated: `python -m cctv_cloud.openapi` writes `openapi.json` and `docs/api-reference.md`.

**The pilot** (plan 10). `GET /v1/pilot/kpis` works out, per branch from `pilot_started_on`, events and review results per day and feature (precision once 20 events of a feature are reviewed), false alarms per day, alert p95 (`alert_deliveries.latency_ms`), and camera and box uptime from `camera_health_minutes`, with each success criterion met, not met or not decidable yet. Count buckets also carry staff crossings left out and queue waits (count, total, longest); reports and the overview show queue figures when the queue feature is on.

**Install and shadow mode** (plan 9). Each heartbeat adds a sample to
`camera_health_minutes` (per camera and minute: samples, degraded, offline, new stream
restarts from the box's counter, lowest fps; kept 60 days). The setup test passes a camera
when, over the window, it never restarted, was never offline and no minute is missing.
While `branches.shadow_until` is in the future, alert rules only fire for users with
`shadow_reviewer` (our support accounts, set by super-admins), and no webhooks or report
links go to the client. `export-reviewed` writes `manifest.csv` and the media of reviewed
events to a folder, recording each file in the audit log (`export_snapshot`/`export_clip`,
`via: cli`); runbooks in `docs/runbooks/`.

**The operational system** (plan 8). Our team adds a sign-in integration for the client
(`POST /v1/sso-integrations` as super-admin with `X-Tenant-Id`): an issuer, unique across
clients, and either HS256 (a secret is made and shown once) or the ops system's RS256/ES256
public key. Tokens must name the issuer, `aud` = `CCTV_SSO_AUDIENCE` (default
`CCTV_WEB_URL`), an invited user's `email`, a `jti`, and live at most
`CCTV_SSO_MAX_TOKEN_SECONDS` (300); each works once (`sso_token_uses`). Owners also need
a second factor in `amr` unless the integration's `require_mfa` is off. Sign-ins and
refusals (with the reason) are audited. Webhooks are queued in the transaction that
stores the event or report, sent after the commit, and retried by the worker
(`CCTV_WEBHOOK_MAX_ATTEMPTS`, 30 s doubling to 1 h, dropped after a day); deliveries are
kept 30 days. `CCTV_WEBHOOK_ALLOW_PRIVATE=true` lets them reach private addresses in dev
(refused in staging and production).

**Email** (password reset, invitations) is written to the log until a provider is chosen.
