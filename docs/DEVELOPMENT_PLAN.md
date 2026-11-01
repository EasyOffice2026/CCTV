# AI CCTV Data Management System — Development Plan

Source: *AI CCTV Data Management System — Developer Brief* (Oct 27, 2026, Ibrahim Javid) and the Hik-Connect screenshot of the Mudawara sites (Oct 27, 2026).
Status: draft v0.1 (2026-10-30). Review this with the client on the kickoff call.

---

## 0. What the screenshot tells us

The screenshot is from the **Hik-Connect** app, so the cameras and NVRs are **Hikvision**. That helps: Hikvision supports RTSP, ONVIF and the ISAPI HTTP API.

| Site (NVR) | Cameras | What's visible | Problems found |
|---|---|---|---|
| **العارضية (Ardiya)** | 8 | Dining area (orange chairs), outdoor entrance/parking | Shows **"Encrypted"** (Hik-Connect video encryption is on). NVR clock is about **4 min ahead** (10:59 overlay vs 10:55 on the phone). |
| **العقيلة (Egaila)** | 8 | Shopfront/sidewalk, kitchen/counter with staff | **"Disk Exception"**: the NVR hard drive has failed or is not formatted, so this branch is probably **not recording right now**. One thumbnail is dated **10-24 Sat 20:18**, three days old, so that camera may be offline. |
| **المعمل (Central kitchen / lab)** | 4 | Kitchen prep line, cold room / storage | NVR clock shows **15:55**, about **5 h ahead**. The time zone is wrong, which would break attendance times. |

**Total: 3 sites, 20 cameras**

What this means for the project:
1. **Which site is the pilot?** The brief says "Mudawara" as if it were one branch, but there are 3 sites. Agree on one pilot branch. The central kitchen (المعمل) is the best place for **kitchen compliance** footage. A branch (Egaila or Ardiya) is needed for **cashier, customer count and queue**.
2. **Fix the Egaila NVR disk before the pilot.** Tell the owner now, because they may not know they have no recordings.
3. **Time sync is required.** Set all NVRs and cameras to NTP with the Asia/Kuwait time zone (UTC+3). The edge device should timestamp events with its own NTP-synced clock and must not read time from the burned-in video overlay.
4. **Every channel is called "Camera 01".** The site survey must produce a camera inventory (channel no., location, view, model, resolution, lens) and give the cameras real names.
5. **The lenses are wide-angle/fisheye and mounted on the ceiling.** This confirms the brief: face recognition needs a **dedicated entrance camera**. Detecting gloves and hair caps from these angles will be hard (see §9 Risks).
6. **"Encrypted"** only matters for viewing through the Hik-Connect cloud. On the local network the edge device pulls RTSP with NVR credentials. Ask for a **dedicated read-only NVR user** for the AI system rather than the admin password.
7. Hik-Connect does **not** give us RTSP over the internet. The AI device must sit **on the branch LAN**, which supports the brief's edge architecture.

---

## 1. Review of the brief — gaps and decisions needed

| # | Topic | Issue | Proposed answer / question |
|---|---|---|---|
| 1 | Pilot scope | "Mudawara" vs 3 sites | Pick 1 branch for the pilot. Add the central kitchen only for compliance data collection. |
| 2 | Roles | "Role-based access: owner" is cut off | Propose roles: **Super-admin (us)**, **Client owner**, **Branch manager**, **Viewer/Reviewer**, **Installer**. |
| 3 | Live view vs bandwidth | The brief sends only events to the cloud, but also wants live view and playback in a web dashboard | Live view = **on-demand WebRTC** from the edge using the camera **sub-stream**, only while someone is watching. Playback of continuous footage = streamed from the **NVR** on demand. Event clips = from cloud storage. |
| 4 | Continuous 30-day storage | Who stores it? | **Keep the existing NVRs** for continuous recording (this is what they already do). The edge device stores only event clips plus a short ring buffer. This saves disk cost on the edge. |
| 5 | Cash drawer detection | Vision alone is unreliable | Pilot: vision on a drawer ROI. Better: **POS integration** (drawer-open signal) and match it with video. Ask which POS they use. |
| 6 | Accuracy "90% per feature" | Not defined | Define it per feature as **precision and recall** on a labelled test set. Gloves/hair cap may need a lower first target (see §9). |
| 7 | Legal | Kuwait privacy | Check with local counsel: CITRA Data Privacy Protection Regulation, and **Kuwait Law No. 61/2015 on surveillance cameras**, which may set a **minimum retention period and signage rules** that override the "30 days" default. |
| 8 | Face recognition | Customer faces must not be stored | Match only against enrolled staff templates. Throw away non-matching faces immediately. Never keep customer embeddings. |
| 9 | Mudawara operational system | No info on its stack or auth | Need: tech stack, auth method (for SSO), and whether embedding (iframe / micro-frontend) is allowed. |
| 10 | WhatsApp | Needs the official API | Use the **WhatsApp Business Platform (Cloud API)** with a verified business number and pre-approved message templates. Approval can take days, so start early. |
| 11 | Cloud region | Data residency | Use the nearest GCC cloud region (e.g. AWS Bahrain `me-south-1` / UAE `me-central-1`) unless the law requires in-country hosting. |
| 12 | Model licences | Commercial product | **Ultralytics YOLO is AGPL-3.0** and **InsightFace pretrained weights are non-commercial**. Buy a licence or use Apache/MIT-licensed models (RT-DETR, YOLOX, D-FINE, etc.). Decide this before training. |

---

## 2. Target architecture

```
 Branch LAN                                         Cloud (multi-tenant)
┌──────────────────────────────────────┐          ┌───────────────────────────────────────────┐
│ Hikvision cameras ──► NVR (24/7 rec)  │          │  API (FastAPI)  ◄──►  PostgreSQL (+RLS)    │
│        │ RTSP main/sub                │  HTTPS/  │      │                    ▲                 │
│        ▼                              │  MQTT    │      ▼                    │                 │
│ Edge AI box (Jetson Orin)             │ ───────► │  Workers (alerts, reports, retention)      │
│  • stream ingest (go2rtc/GStreamer)   │ events,  │      │                                     │
│  • detectors + tracker                │ clips,   │      ▼                                     │
│  • rules engine (zones, schedules)    │ heartbeat│  Object storage (clips, snapshots)          │
│  • ring buffer → clip/snapshot        │          │  Redis (pub/sub, queues)                    │
│  • local outbox (offline buffer)      │ ◄─────── │  WebSocket gateway ──► Web dashboard        │
│  • WebRTC live view on demand         │ config,  │  WhatsApp Cloud API, Email                  │
│  • VPN tunnel (WireGuard/Tailscale)   │ OTA      │  Admin panel · Mudawara ops-system embed    │
└──────────────────────────────────────┘          └───────────────────────────────────────────┘
```

### Recommended stack

| Layer | Choice | Why |
|---|---|---|
| Edge hardware | **Jetson Orin NX 16GB** for 8–16 cams. **Orin Nano Super 8GB** for the 4-cam kitchen | Hardware video decode, enough TOPS for several models at 5–10 fps per camera |
| Edge runtime | JetPack 6, Docker Compose, **DeepStream** or GStreamer + TensorRT | Hardware decode and fast inference |
| Edge app | Python 3.11 service (rules, events, sync), SQLite outbox | One language with the backend |
| Streaming | **go2rtc** or MediaMTX (RTSP restream + WebRTC) | One RTSP pull per camera, shared by AI and live view |
| Detection | Person/phone detector (licence-safe YOLO-family or RT-DETR), **ByteTrack** tracking, custom PPE model | |
| Face | SCRFD-style detector + ArcFace-style embedder (**commercially licensed weights**), **or** a Hikvision face-recognition terminal at the staff door reporting through ISAPI | The terminal option is cheaper and more accurate for attendance |
| Backend | **FastAPI**, SQLAlchemy, Alembic, Celery/ARQ workers | |
| DB | **PostgreSQL 16** with `tenant_id` on every row + **Row-Level Security** | Required tenant isolation |
| Storage | S3-compatible object storage, prefixed by tenant, lifecycle rules for retention | |
| Realtime | Redis pub/sub → WebSocket; MQTT (EMQX/Mosquitto) or HTTPS for edge↔cloud | |
| Frontend | **Next.js + React + TypeScript**, TanStack Query, Tailwind, Konva/Fabric for zone drawing | |
| Reports | WeasyPrint (PDF), openpyxl (Excel) | |
| Auth | Own JWT auth + **TOTP 2FA**, OIDC endpoint for Mudawara SSO (or Keycloak) | |
| Ops | GitHub Actions, Docker registry, Terraform, Prometheus + Grafana, Sentry | |

### Repository layout (monorepo)

```
CCTV/
├── contracts/        # shared edge↔cloud API models (v1) + JSON Schemas
├── edge/
│   └── cctv_edge/    # Jetson service: ingest, ring buffer, detection, rules, clips, outbox, sync
├── cloud/
│   ├── cctv_cloud/   # FastAPI app (api/), DB models (db/), later workers/
│   └── migrations/   # Alembic
├── web/              # Next.js dashboard + admin panel
├── ml/               # dataset tools, labelling exports, training & evaluation scripts
├── infra/            # Terraform, docker-compose, edge provisioning
└── docs/             # this plan, API docs, onboarding checklist, runbooks
```

---

## 3. Core data model (first version)

```
tenants(id, name, plan, retention_media_days=90, retention_event_days=365)
branches(id, tenant_id, name, timezone='Asia/Kuwait', address)
working_hours(id, branch_id, weekday, open_time, close_time)
edge_devices(id, tenant_id, branch_id, serial, version, last_seen_at, status)
nvrs(id, branch_id, vendor, host, credentials_ref)
cameras(id, tenant_id, branch_id, nvr_id, channel, name, rtsp_main, rtsp_sub, role, status)
zones(id, tenant_id, camera_id, name, type, polygon_json)        -- counter, kitchen, restricted, entrance_line...
features(key, name, category, default_enabled)                    -- attendance, kitchen_ppe, cashier, ...
feature_settings(id, tenant_id, scope{tenant|branch|camera}, scope_id, feature_key, enabled, params_json)
staff(id, tenant_id, branch_id, employee_code, name, shift_id, consent_signed_at)
face_templates(id, tenant_id, staff_id, embedding_encrypted, model_version)
events(id, tenant_id, branch_id, camera_id, zone_id, feature_key, event_type,
       started_at, ended_at, confidence, staff_id NULL, track_id,
       snapshot_key, clip_key, review_status{new|valid|false_alarm|reviewed},
       reviewed_by, reviewed_at, dedup_key, metadata_json)
attendance_records(id, tenant_id, staff_id, date, check_in, check_out, status{on_time|late|absent})
counts(id, tenant_id, camera_id, bucket_start, in_count, out_count, occupancy)  -- 5-min buckets
users(id, tenant_id NULL, email, role, totp_secret_enc, ...)
user_branch_access(user_id, branch_id)
alert_rules / alert_subscriptions(user_id, feature_key, severity_min, channels{web,whatsapp})
alerts_sent(id, event_id, user_id, channel, sent_at, status)
audit_logs(id, tenant_id, user_id, action{view_live|view_clip|export|settings_change}, target, at, ip)
```

This sketch is now implemented in [cloud/cctv_cloud/db/models](../cloud/cctv_cloud/db/models). Table and column names there are the source of truth: `counts` became `count_buckets`, `alerts_sent` became `alert_deliveries`, and `dedup_key` was dropped because the edge's `event_id` is the idempotency key.

**Feature on/off resolution:** camera setting → otherwise branch → otherwise tenant → otherwise default. The edge device receives the *resolved* config, so a feature that is off never runs inference.

---

## 4. Step-by-step plan

Assumes about **2 developers** (1 backend/frontend, 1 CV/edge). One developer alone would take roughly 1.5–2× as long. Weeks overlap where the work is independent.

### Phase 0 — Discovery & site survey (Week 1–2)

- [ ] **0.1 Kickoff call** with Ibrahim. Go through §1 and agree on: pilot branch, roles, live view approach, the 4 first features, success metrics, and the Mudawara ops-system integration method.
- [ ] **0.2 Site survey** (pilot branch + central kitchen):
  - Camera inventory sheet: NVR model/firmware, channel → location → purpose → resolution/fps/codec (H.264/H.265) → lens.
  - Check RTSP on the LAN: `rtsp://<user>:<pass>@<nvr-ip>:554/Streaming/Channels/<ch>01` (main) and `<ch>02` (sub). Confirm ONVIF is enabled.
  - Create a **read-only NVR user** for the AI system.
  - Network: LAN layout, free switch port for the edge box, upload speed test, router access for an outbound VPN.
  - Staff entrance: lighting, mounting point, power/PoE for a face camera.
  - Cash counter: is there a camera that sees the drawer from above? Which POS is used?
  - Photograph each view and mark candidate zones.
- [ ] **0.3 Fix site hygiene:** NTP and time zone on every NVR, replace the Egaila disk, rename channels.
- [ ] **0.4 Start footage collection now.** Export 3–7 days of footage per relevant camera (kitchen, counter, entrance, dining) covering peak, quiet and closed hours. This is the training/test data, and labelling takes the longest.
- [ ] **0.5 Legal & consent:** staff consent form draft, CCTV notice sign text, check retention law with counsel.
- [ ] **0.6 Order hardware:** 1–2 Jetson Orin units (+ NVMe SSD 1TB, UPS), 1 face camera or face terminal for the staff entrance.
- [ ] **0.7 Decide model licences** (§1 #12).

**Exit:** signed-off scope, camera inventory, RTSP confirmed working, footage collected, hardware ordered.

### Phase 1 — Foundations (Week 2–3)

- [x] 1.1 Monorepo, CI (lint, tests, Docker build), pre-commit. *Branching rules still to agree; type-checking (mypy) not added yet.*
- [x] 1.2 Dev environment: `docker-compose` with Postgres, Redis, MinIO (local S3), MQTT, API. *The web dashboard joined the stack in Phase 4.*
- [x] 1.3 **Simulated cameras:** loop the collected footage as RTSP streams (MediaMTX + ffmpeg) so all development works without being on site.
- [ ] 1.4 Cloud infrastructure (staging) with Terraform: Postgres, object storage, container hosting, secrets manager, TLS. *Blocked on the cloud provider/region decision (§1 #11).*
- [x] 1.5 DB schema v1 (§3) + migrations + **RLS policies** + a test that proves tenant A cannot read tenant B.
- [x] 1.6 Edge↔cloud contract: event JSON schema, clip upload flow (pre-signed URLs), heartbeat, config pull. Version it (`/v1`).

**Exit:** `docker compose up` runs the whole stack on a laptop with simulated cameras.

### Phase 2 — Edge core (Week 3–6)

- [x] 2.1 Jetson provisioning script: JetPack, Docker, NVMe, VPN agent, auto-start, watchdog (`infra/edge/provision.sh`). *Not yet run on real Jetson hardware. VPN uses Tailscale; switch if WireGuard is chosen.*
- [x] 2.2 **Stream ingest:** each stream is pulled once (main → ring buffer, sub → AI; main at reduced fps if there is no sub-stream); reconnect with backoff; per-camera health (online/degraded/offline, fps). *Decoding uses ffmpeg on the CPU for now; Jetson hardware decode comes with the model in Phase 5. go2rtc is added with live view (Phase 4).*
- [x] 2.3 **Ring buffer:** the main stream is copied (no re-encode) into 2 s segments named by UTC start time, kept for 4 min, so a clip can include **pre-event** footage (10 s before + 10 s after by default).
- [ ] 2.4 **Base pipeline:** decode → detection → tracking (track IDs) → zone tests (point-in-polygon on foot point, ignore zones) → line crossing. *Done except the **person detector model**, which needs the model/licence decision (§1 #12) and comes in Phase 5. A motion detector (no weights) works now. The IoU tracker is replaced by ByteTrack along with it.*
- [x] 2.5 **Rules engine:** turns detections into events using config — zone, schedule (working hours / after-hours), min duration, cooldown. **Deduplication:** one ongoing event per track/zone, with an "ended" update instead of repeats. Events longer than 2 min are split so clips stay small. *Security rules only so far; other features add their rules in Phase 5.*
- [x] 2.6 **Clipper:** event → MP4 clip (H.264, browser-playable) + JPEG snapshot with the bounding box drawn. Falls back to a clip made from the snapshot when there is no footage.
- [x] 2.7 **Outbox & sync:** events stored in SQLite first, sent to the cloud with retry/backoff, so **nothing is lost when the internet is down**. Local disk quota with oldest-first cleanup.
- [x] 2.8 **Config sync:** pull the resolved feature settings, zones and schedules; apply without restart; only run pipelines for enabled features. Last config is kept on disk for offline reboots. *Polls every 60 s and when the heartbeat reports a newer version; MQTT push comes with 3.4.*
- [x] 2.9 Heartbeat + metrics (FPS per camera, GPU/CPU temp, disk, queue length); clock-drift warning against the cloud.
- [x] 2.10 OTA updates: pull a signed Docker image tag set from the cloud; roll back if health fails. *Registry: GitHub Container Registry. Signing: cosign keyless, by the release workflow on a `v*` tag (no key to keep). Super-admins add a release (version + image digest from the workflow summary) under Administration › Edge software and roll it out to one box, a client or everyone; the box is offered it with its heartbeat. On the box, `cctv-edge-update.timer` (every minute, as root, outside the container) pulls the image by digest, checks it comes from the box's own repository and is signed by our workflow, switches, and switches back by itself if the new version hasn't sent a heartbeat after 2 minutes and within 10, or keeps restarting. Reports (updating, updated, failed, rolled back) come back with heartbeats; a failed release isn't retried until it's rolled out again. Tested with a stand-in for Docker and in a browser; not yet run on a Jetson or against the real registry.*

**Exit:** on the simulated cameras, an "after-hours person" event appears in the cloud DB with clip and snapshot in under 10 s, and is buffered and synced correctly when the network is cut.
*Status: verified in automated tests against a fake cloud (real ffmpeg ring buffer and clip, scripted person detections, network cut). The real check needs the cloud event API (3.5) and the person model (Phase 5).*

### Phase 3 — Cloud platform core (Week 4–7)

- [x] 3.1 Auth: login, JWT + refresh (rotated, reuse ends the session), **TOTP 2FA** (required for owner/admin, replay-protected), password reset, idle and absolute session timeout, lockout after 5 failures. *Emails go to the log until an email provider is chosen.*
- [x] 3.2 RBAC: roles from §1 #2, branch-level access, checks on every endpoint (plus RLS as a second safety layer). *The role matrix is in `cloud/README.md`; confirm it with the client (§1 #2).*
- [x] 3.3 Admin API: tenants, branches, cameras, NVRs, zones, edge devices (register with a one-time pairing code; `infra/edge/provision.sh` pairs the box), users (invited by email).
- [x] 3.4 Settings API: feature on/off per tenant/branch/camera, zones (polygons), working hours, thresholds (`params`). Changes bump a per-branch config version and publish on Redis; the edge also sees it in the next heartbeat. *An MQTT bridge for instant push is still to do; today the edge picks changes up within 30 s.*
- [x] 3.5 Event ingest API: idempotent by `event_id`, validates every camera/zone/staff/media key against the device's branch, stores media keys, publishes to Redis.
- [x] 3.6 **Search API:** filter by date range, branch, camera, zone, event type, severity, status, staff, review status; cursor-paginated; indexes per §3 plus `(tenant_id, branch_id, started_at)`.
- [x] 3.7 Media access: 5-minute pre-signed URLs; **every view/export written to `audit_logs`**; downloading needs export rights.
- [x] 3.8 Retention worker: media older than the tenant's media retention, then event records older than its event retention; offline-device detection. Lifecycle rule (400 days) as a backstop in the dev stack. *Defaults changed to match §6: media 90 days, events 365 days.*
- [x] 3.9 OpenAPI docs (`/docs`) + API keys with scopes for POS/HRM integrations.

**Exit:** API test suite green, including tenant-isolation and permission tests.
*Status: 69 cloud tests pass against PostgreSQL 16 (object storage, Redis and email replaced by fakes). Not yet run against real MinIO/Redis because Docker isn't available on the dev machine used.*

### Phase 4 — Web dashboard v1 (Week 6–9)

- [x] 4.1 App shell: login + 2FA (QR setup), branch switcher, role-aware navigation, English + **Arabic (RTL)** support (the site names are Arabic). *Static Next.js export behind nginx; refresh token in an HttpOnly cookie, access token in memory only.*
- [x] 4.2 **Event feed:** live (WebSocket `/v1/ws`), snapshot cards, filters, a one-click clip player, buttons for **Valid / False alarm / Reviewed**.
- [x] 4.3 **Search page:** all filters from 3.6, results grid, open clip, export (CSV, audited).
- [x] 4.4 **Live view:** grid per branch, WebRTC through the edge (sub-stream, go2rtc on the box), opened on demand, auto-closes after 5 min of inactivity or when the tab is hidden. *Signalling goes through the API (edge long-poll relay); video flows box → browser. Not yet tried against a real box/NVR; viewers outside the branch network will need a TURN server.*
- [x] 4.5 **Playback:** timeline per camera from the NVR (ISAPI search + RTSP playback through the edge), with event markers on the timeline. *Hikvision ISAPI/RTSP formats implemented from the documentation; to verify on the site NVRs (and their clock/time-zone settings, see §0).*
- [x] 4.6 **Settings:** draw/edit zone polygons and count lines on a live camera frame; feature toggles per tenant/branch/camera; working hours; alert thresholds (per-feature JSON for now; friendly forms per feature come with Phase 5 rules).
- [x] 4.7 **Admin panel:** clients, branches, cameras/NVRs, devices (online/offline, versions, config up to date, pairing codes), users (invite, roles, branch access, 2FA reset).
- [x] 4.8 Staff management: add staff, consent checkbox + date, enrol face (photos embedded on the edge box, only the encrypted embedding stored), shifts. *Enrolment works end to end once the face model is on the box (Phase 5, §1 #12); until then the box answers "face model not installed".*

**Exit:** a non-developer can set up a branch, draw zones, toggle features and review events without touching code.
*Status: verified in a browser (Playwright, against the real API and PostgreSQL with a simulated edge box): sign-in with 2FA setup, live event arriving over the WebSocket, review, Arabic/RTL, zone drawing on a frame from the box, staff consent + face enrolment, CSV export, playback timeline. Live video itself needs a real box (go2rtc) and wasn't exercised. To confirm with the client: the role matrix (live view for branch managers?).*

### Phase 5 — Pilot AI features, wave 1 (Week 5–12, alongside the work above)

For **each** feature follow the same loop: **label → train/configure → offline evaluation on held-out footage → shadow mode on site → tune → enable alerts.**

- [ ] **5.0 Labelling setup:** CVAT or Label Studio; labelling guide per feature; split footage into train/val/**test** (the test set is never trained on and is used for the 90% check).
  *Tooling ready (`ml/`): Label Studio configs for person boxes, PPE attributes, cash drawer and event intervals; labelling guides with privacy rules; a split tool that keeps each camera-day in one split (and keeps test as test when footage is added). Waiting on: footage (0.4) and annotators.*

- [ ] **5.1 After-hours security** *(easiest — build first)*
  - Person detection + tracking in "restricted" zones or anywhere outside working hours.
  - Ignore zones (TV screens, glass reflections, the street seen through windows).
  - Minimum dwell time (e.g. 3 s) to stop flicker false alarms; severity = critical → WhatsApp.
  - *Software ready: ONNX person detector (YOLOX or RT-DETR/D-FINE exports, loaded from `/opt/cctv-edge/models` with a `model.json` manifest that must state the licence), ByteTrack-style tracker, ignore zones, dwell/cooldown per rule. Waiting on: the model choice (§1 #12) and evaluation on Mudawara footage. WhatsApp comes with Phase 6.*
- [ ] **5.2 Cashier area**
  - Counter zone: "unattended counter" = no staff in the counter zone for more than N seconds during working hours.
  - Drawer-open: small classifier on the drawer ROI (open/closed) from top-down footage. Log each opening as an event; later match it against POS transactions ("drawer open without sale").
  - Counter activity: occupancy timeline for reports.
  - *Software ready: "counter unattended" absence rule (60 s, open hours), drawer open/closed classifier on each `cash_drawer` zone (every opening is an event), counter occupancy in 5-minute buckets (`occupied_seconds`, `max_occupancy`) sent to the cloud (`/v1/edge/counts`, `GET /v1/counts`). Waiting on: a drawer classifier trained on top-down counter footage; POS matching is a later phase.*
- [ ] **5.3 Kitchen compliance (mask / gloves / hair cap)**
  - Person detection → crop → **PPE classifier/detector trained on Mudawara footage** (central kitchen + branch kitchen).
  - Only judge people inside the kitchen zone, and only when the crop is large and sharp enough (otherwise "unknown", not a violation).
  - Violation must persist across several frames/seconds before it becomes an event; one event per person per period.
  - Set the hygiene rules with the client (which items are required in which zone).
  - *Software ready: person crop → PPE attribute classifier (mask/gloves/hair cap), quality gate (crop height, sharpness) that answers "unknown" instead of guessing, 5 s persistence, one event per person per 15 min; required items = the rules switched on per branch/camera. Waiting on: a PPE model trained on Mudawara kitchen footage, and the client's hygiene rules.*
- [ ] **5.4 Staff attendance (face recognition)**
  - Option A (recommended for the pilot): **Hikvision face-recognition terminal** at the staff entrance; pull its check-in events via ISAPI.
  - Option B: dedicated 4 MP camera + edge face pipeline (detect → quality check → embed → match against enrolled templates with a threshold).
  - Enrolment: 3–5 images per staff member under the site lighting; store **encrypted embeddings only**, no photos.
  - Rules: first seen = check-in, last seen = check-out; compare with shift → late/absent; manual correction screen for managers.
  - Non-matching faces are thrown away right away (privacy rule).
  - *Software ready (option B): face analyzer on the edge (OpenCV YuNet + SFace, MIT/Apache-2.0, behind the same manifest), enrolment from photos through the box, templates pulled into memory only (`/v1/edge/face-templates`), sightings (`/v1/edge/attendance`) turned into work-day records in the cloud: check-in/out, on time/late against the shift, overnight shifts, absences after the shift (worker), and a manager correction screen (Attendance page, audited, undoable). Option A (Hikvision terminal via ISAPI) can feed the same records once the hardware is chosen (0.6). Waiting on: the face camera/terminal, tuning the match threshold on site.*
- [ ] 5.5 Per-feature evaluation report: precision, recall, false alarms per day on the test set, plus a confusion review of failures.
  *Tooling ready: `python -m cctv_edge.replay` runs the real edge pipeline over recorded footage; `python -m cctv_ml.evaluate` compares its events with labelled intervals (precision, recall, F1, false alarms per camera-day, start delay, threshold sweep, 90% pass/fail, list of failures); `cctv_ml.classify_eval` does the same for crop classifiers. Waiting on: labelled test footage and trained models.*

**Exit:** each wave-1 feature hits its offline target (or has an agreed adjusted target) before shadow mode.
*Status: all wave-1 software is in place and tested (edge rules, model runtime, tracker, attendance, counts, replay and evaluation tools, cloud attendance and counts, dashboard attendance page). No model weights are in the repo; the exit check needs labelled Mudawara footage and models chosen under §1 #12. Per-feature thresholds are editable in Settings › Features (defaults shown there).*

### Phase 6 — Alerts & reports (Week 9–12)

- [x] 6.1 Alert router: event → matching subscriptions (user, feature, severity, branch) → channels. *Routed in the ingest transaction (one queued delivery per user and channel, permission and branch access re-checked), sent right after the commit, retried by the worker with backoff.*
- [x] 6.2 Browser notifications (Web Push + in-app toast) with snapshot and clip link. *Toast over the WebSocket to that user only; Web Push (VAPID, own RFC 8291 encryption) via `public/sw.js` when no tab is in front; both link to the event (snapshot and clip open there, audited). Needs `CCTV_VAPID_PRIVATE_KEY` in each environment.*
- [ ] 6.3 **WhatsApp Cloud API**: templates (e.g. `after_hours_intrusion`) approved by Meta, a short link to the clip (login required), rate limit. *Software done: template messages (`cctv_alert`, `cctv_alert_resolved`, `cctv_report`, English and Arabic; text in `cloud/README.md`), link button to the event, number + recorded consent per user, 20 per user per hour, signed delivery-report webhook. Waiting on: the verified business number, the token and Meta's template approval (§1 #10).*
- [x] 6.4 **No repeat alerts:** one alert per ongoing event plus a cooldown per camera/zone/feature; send an optional "resolved" message. *Unique per event/user/channel/kind; 5-minute cooldown per camera, zone and event type; "resolved" per rule; events arriving > 30 min late don't alert.*
- [x] 6.5 Per-user alert preferences page (features, severity, quiet hours, channels). *Alerts page: rules (feature, branch, severity, channels, quiet hours with critical alerts still sent, "resolved"), WhatsApp number and consent, message language, report links, browser notifications, test buttons.*
- [x] 6.6 **Reports:** daily branch summary (attendance, compliance issues, customer count, peak hours, security events), weekly/monthly trends, staff scorecard (attendance %, compliance score). Scheduled generation, then PDF/Excel download and an email/WhatsApp link. *Reports page and API; worker makes daily (after 02:00 branch time), weekly (Sunday start) and monthly reports and sends links; PDF and Excel in English or Arabic, audited; kept 2 years (§6). Compliance per person only counts events matched to a person, which kitchen events aren't yet (the PPE pipeline doesn't identify people).*
- [x] 6.7 Measure alert latency (event start → notification delivered) and store it per alert. This proves the "< 10 s" target. *`latency_ms` per delivery (event start to accepted by the channel) and `delivered_at` from WhatsApp delivery reports; shown per event and as p50/p95/% within 10 s in every report.*

*Status: tested against PostgreSQL with fake channels (routing, cooldown, quiet hours, retries, rate limit, push encryption round trip, webhook signatures, report figures and files, scheduling), and in a browser (rule set-up, live toast within seconds of an edge event, event link with the deliveries, report with PDF and Excel downloads, Arabic). Not yet exercised: real WhatsApp delivery, real browser push services (needs a VAPID key and HTTPS), and an email provider (emails still go to the log).*

### Phase 7 — Security, privacy & compliance (Week 3–13, alongside the work above; finish by Week 13)

- [x] 7.1 TLS everywhere; edge↔cloud with per-device credentials (mTLS or device tokens), rotated. *Device tokens (not mTLS): rotated every 30 days by the box on the heartbeat's request, 10-minute grace period, ended by re-pairing or retirement. The box refuses a plain-http cloud URL; the API refuses plain-http dashboard and storage URLs in staging/production; HSTS and security headers on every response. TLS termination itself is the load balancer's (Terraform, 1.4).*
- [x] 7.2 Encryption at rest: DB (managed encryption), object storage (SSE), edge NVMe (LUKS), face embeddings encrypted at the application level with KMS. *Face templates: AES-256-GCM bound to tenant and person, with an AWS KMS data key per template (`CCTV_FACE_KEY=aws-kms:…`) or a local key; rotation command. `security-check --online` fails without bucket encryption. `ENCRYPT_NVME=yes` in the provisioning script (LUKS2, key on the eMMC). Database encryption is a setting of the managed service (Terraform, 1.4). Not yet run against real KMS.*
- [x] 7.3 Audit log UI (who viewed/exported what, when) for owners and super-admin. *Audit log page: names, filters (action, person, target, dates), event links, CSV export (itself logged); platform log for super-admin sign-ins; entries kept 2 years, never less than 1, append-only for the app.*
- [x] 7.4 Secrets: no NVR or camera passwords in git or the DB in plain text (use a secrets manager). *Camera passwords only on the box (600, warned if wider); database and API reject RTSP URLs with credentials; cloud secrets from `CCTV_SECRETS_DIR` files (secrets-manager mounts); gitleaks in CI and pre-commit; `.gitignore` for credential and token files.*
- [x] 7.5 Consent workflow: no face enrolment without recorded consent; delete a staff member's biometric data when they leave. *API and database trigger; deletion on "left" or withdrawal, box drops the template within a minute; consent form text in `docs/privacy/`.*
- [x] 7.6 Pen-test checklist (OWASP Top 10, tenant-isolation tests, IDOR on clip URLs). *`docs/security.md`: automated sweeps in CI (every route needs credentials; every id-taking route tried across tenants and across branches with valid bodies; RLS; 2FA lockout; token rotation; template tampering) plus the manual list for before go-live.*
- [x] 7.7 Privacy documentation: data-flow diagram, retention table, consent form, signage text. *`docs/privacy/` (English and Arabic for the form and signs); for the client's counsel to confirm (Law 61/2015 retention, biometric consent).*

*Status: 17 new cloud tests and 5 edge tests pass (token rotation end to end with the real edge client, KMS with a stand-in, audit log API and purge, 2FA lockout, the route sweeps). Still open: Terraform (1.4) is where RDS encryption, the KMS key, the bucket policy and the secrets mounts are defined; Jetson flash-time disk encryption (Phase 9); a `rotate-secret-key` command; the manual pen-test before the pilot.*

### Phase 8 — Mudawara operational system integration (Week 11–13)

- [ ] 8.1 Agree on the method: **SSO (OIDC / signed JWT handoff)** + embed the dashboard pages (iframe with CSP `frame-ancestors`, or a micro-frontend). *Software ready for the signed-JWT handoff (it works with any stack, and an OIDC provider can issue the same tokens): the ops system's server signs a 2-minute, single-use token per user (HS256 secret we generate, or its RS256/ES256 public key); `/sso/#token=…` opens the dashboard, `/embed/overview/#token=…` in an iframe, limited by `frame-ancestors` to the origins in `EMBED_ANCESTORS`. Users are matched by email and must be invited; owners need 2FA on the ops side (`amr`). Guide for their developers: `docs/integration/ops-system.md`. Waiting on: their technical contact to confirm the method, issuer, key, page origins, and whether they have 2FA for owners (§1 #9).*
- [x] 8.2 "Owner overview" page built for embedding: attendance, compliance, customer count, peak hours, alerts, reports, clips. *Overview page (also the owner's and manager's home page) and `/embed/overview/` for the iframe: per branch, today so far: events, issues, attendance, customers by hour and peak, cameras and edge boxes, alert times, latest events with snapshot and clip, latest reports; English and Arabic; refreshes every minute; tells the parent page its height and when it needs a new token.*
- [x] 8.3 The same feature toggles apply (it is the same backend). *A feature off on every camera of a branch has no section and its events aren't listed, in the page and in the API.*
- [x] 8.4 Optional: REST endpoints/webhooks so the ops system can pull KPIs into its own screens. *`GET /v1/overview` with an API key scoped `kpis:read` (totals only, nothing else opens); signed webhooks for new and finished events and new reports (HMAC-SHA256, retried for a day, public https only, no images or names); Integrations page for API keys, webhooks (test, recent sends) and the sign-in setup.*

*Status: 13 new cloud tests (sign-in tokens: replay, expiry, audience, wrong key, unknown and cross-tenant users, owner 2FA, algorithm pinning, RS256/ES256; overview toggles and permissions; API key scope; webhook signing, retries, private addresses, reports) and the route sweeps over the new endpoints pass. In a browser: the sign-in link, the overview embedded in a page on another origin (Arabic, postMessage token, event clip), API key and webhook to a local receiver with the signature checked. Not yet done with the real ops system (8.1).*

### Phase 9 — On-site install & shadow mode (Week 12–14)

- [ ] 9.1 Install the edge box at the pilot branch (UPS, wired LAN, VPN up), connect all cameras, register in the admin panel. *Ready: install runbook (`docs/runbooks/install.md`: NVR fixes from §0, provisioning and pairing, credentials, checklist) and the site check on the box, `python -m cctv_edge --check`: pairing, cloud, clock against the cloud, disk, temperature, every camera's main and sub-stream against the minimum spec, and each NVR's clock, time zone, time source and disks (the survey's "4 min / 5 h off" and "Disk Exception"). Waiting on: the visit.*
- [ ] 9.2 **Setup test** (brief test #1): all cameras streaming, 48 h without dropped streams. *Ready: the box counts stream restarts per camera; the cloud keeps health per camera and minute (60 days); Edge devices page shows the test per camera (restarts, minutes offline, minutes without heartbeat, longest outage, hour bars) from the first heartbeat, with pass/fail. Waiting on: 48 h on site.*
- [ ] 9.3 Draw real zones, set working hours, enrol staff (with consent). *Existing pages; steps in the install runbook. Waiting on: the visit and signed consent forms.*
- [ ] 9.4 **Shadow mode (1–2 weeks):** events are recorded but **alerts go only to us**. Review every event daily and mark valid/false. *Ready: shadow mode per branch until a date (super-admins); alerts only to accounts marked "Support team", no webhooks or report links to the client, a banner on every page. Daily review page: the day's unreviewed events one by one with clip, keys V/F/R/S/B. Routine in `docs/runbooks/shadow-mode.md`.*
- [ ] 9.5 Tuning loop: adjust thresholds, ignore zones, dwell times; retrain on the false alarms collected (hard-negative mining). *Ready: review figures per event type, camera and zone (precision, false alarms/day, confidence of valid vs false) on the review page and `GET /v1/review-summary`; `cli export-reviewed` (events, notes and media for labelling, audited) and `cctv_ml.review_sweep` (threshold per event type from the reviews). Training itself waits on the model decision (§1 #12).*
- [ ] 9.6 Train the owner and managers (review flow, settings, alerts). *Ready: guide in English and Arabic, `docs/training/owner-and-managers.md`. Waiting on: the session.*

*Status: the software and documents for Phase 9 are built and tested (edge site check against simulated NVR replies and probes; health per minute, setup test, shadow-mode routing, review figures and export against PostgreSQL; threshold sweep; in a browser: shadow banner, keyboard review, tuning table, setup test). The items stay open until they are done on site.*

### Phase 10 — Official 30-day pilot (Week 14–18+)

- [ ] 10.1 Turn on wave-1 alerts for the client: attendance, kitchen compliance, cashier, after-hours security. *Day-1 checklist in `docs/pilot/README.md` (end shadow mode, set pilot day 1, features and thresholds from shadow mode, alert rules and test messages). Waiting on: the end of shadow mode.*
- [ ] 10.2 **Wave 2 once wave 1 is stable (~week 2 of the pilot):**
  - **Customer count:** entrance line crossing (in/out), occupancy, busy hours. Exclude staff (staff-zone heuristics, uniform classifier, or face-matched staff).
  - **Queue detection:** people in the queue zone over time; wait time ≈ time in queue (tracking) or queue length ÷ service rate; alert above a threshold.

  *Software ready: people seen in a staff area, kitchen or counter zone of the camera are left out of entrance counts and of queue and dining occupancy (crossings left out are reported); dining occupancy is counted and `occupancy_high` fires at `min_count` people (default 30, per camera); `queue_long` fires at `min_count` people (5) for 60 s; each person's time in a queue zone gives average and longest waits per bucket; figures in the overview, reports and the KPI API. Known limit: staff never seen in a staff zone are still counted (a uniform classifier would catch them). Waiting on: wave 1 stable, zones drawn, a manual count to check against.*
- [x] 10.3 Daily KPI dashboard for the pilot itself: accuracy (from review labels), false alarms/day, alert latency, uptime. *Pilot page (and `GET /v1/pilot/kpis`): from pilot day 1, per day and in total: events, reviewed, precision per feature (judged from 20 reviewed events), false alarms per day, alert p95, camera and edge-box uptime from the heartbeats, pilot day; the success criteria with met / not met / not enough data. Recall comes from the offline test-set report.*
- [ ] 10.4 Weekly check-in with the owner; keep a feedback log. *Pilot log on the Pilot page (check-ins, feedback, changes, issues, decisions; per branch or client-wide; owners and our team write, managers read their branches); weekly agenda in `docs/pilot/README.md`. Waiting on: the pilot.*
- [ ] 10.5 **Success criteria sign-off** (brief §Testing):

| Check | Target | How we measure |
|---|---|---|
| Detection accuracy | ≥ 90% per feature | Precision & recall on the labelled test set + reviewed live events |
| False alarms | < 5 per branch per day | Events marked "false alarm" ÷ days |
| Alert time | < 10 s | Stored latency per alert (p95) |
| Uptime | ≥ 99% | Edge heartbeat + per-camera stream health |
| Search | Find a specific event/clip in < 30 s | Scripted user test |
| Pilot period | ≥ 30 days | Continuous run |
| Feedback | Owner confirms usefulness | Sign-off form |

If a feature misses its target, extend the pilot for that feature only. Don't hold back the features that passed.

*Sign-off form (English and Arabic) with the search test procedure: `docs/pilot/sign-off.md`. Status of Phase 10: wave-2 software, the pilot figures and the log are built and tested (edge counting and rules, cloud figures against PostgreSQL, the Pilot page in a browser); the pilot itself hasn't run.*

### Phase 11 — Productisation & handover (Week 18–20)

- [x] 11.1 **Onboarding checklist** (survey → camera check → connect → zones → go live) as a document and as an admin-panel wizard. *`docs/runbooks/onboarding.md`, and Administration › Onboarding: 11 steps per branch worked out from what is set up (hours, cameras, box online, streaming, setup test, zones, features, staff with consent, alert rules, shadow mode, pilot day 1), the next one highlighted, each linked to its page.*
- [x] 11.2 **Hardware kits:** *`docs/hardware.md`. To confirm with the pilot box's measured load: video decoding is still on the CPU (hardware decoding on the Jetson is planned), so the camera counts per kit are design targets.*
  - Small (≤ 8 cams): Orin Nano Super / Orin NX 8GB, 512GB NVMe, UPS
  - Medium (9–16 cams): Orin NX 16GB, 1TB NVMe, UPS
  - Large (> 16 cams): 2 × medium, or AGX Orin
  - Plus a face camera/terminal per staff entrance
- [x] 11.3 Minimum camera spec sheet (1080p @ 15 fps, H.264/H.265, RTSP/ONVIF) + compatibility list (Hikvision, Dahua, ...). *`docs/hardware.md`; the box's site check tests each camera against it. Hikvision is built for (RTSP and ISAPI); Dahua, Uniview, Axis and other ONVIF cameras should work for analysis and live view over RTSP but haven't been tested; NVR playback is Hikvision only.*
- [x] 11.4 Monitoring & on-call: alerts when an edge device goes offline, a stream drops, a disk fills or the GPU overheats. *Heartbeats carry disk, temperature, CPU/GPU and upload backlog; the worker's health check (every minute) opens and resolves incidents (box offline, stream down 5 min, disk under 20 GB, 85 °C, 1,000 waiting); on-call by email and/or a Slack/Teams webhook once an incident lasts 5 minutes, and when resolved; System health page (all clients) and incidents on the Edge devices page; `GET /metrics` for Prometheus (token). Not yet connected to a real on-call channel.*
- [x] 11.5 Docs: architecture, API reference, deployment runbook, edge recovery runbook, model retraining guide. *`docs/architecture.md`, `docs/api-reference.md` (generated from the API with `python -m cctv_cloud.openapi`; a test fails when stale), `docs/runbooks/deploy-cloud.md` (with a tag-triggered release workflow, not run yet), `docs/runbooks/edge-recovery.md` (one section per incident kind, linked from on-call messages), `docs/runbooks/retraining.md`.*
- [ ] 11.6 Source code handover + a recorded walkthrough. *Checklist, accounts and secrets to transfer, walkthrough agenda and open items: `docs/handover.md`. Waiting on: the receiver and the session.*

*Status: monitoring, the onboarding wizard and the generated API reference are built and tested (incidents, paging, short-incident suppression, /metrics, the checklist; in a browser: the wizard and the incidents panel). Documents written for hardware, architecture, deployment, recovery, retraining and handover.*

### Later phase (after pilot)

- [x] Heatmaps (track positions are already available → aggregate into a floor-plan grid) *Built per camera, over the camera's own picture (32 x 18 cells), not yet on a floor plan: the box adds up the seconds people (not staff) stood in each cell and sends one bucket per hour (numbers only); Monitoring › Heatmap shows a camera over today, yesterday or the last 7, 30 or 90 days, on a current camera picture if the user may see one, with the busiest areas of the picture as a list. Turned on per camera with the Heatmap feature. A combined floor plan per branch (mapping each camera onto a drawing) and heatmaps in the reports are still to do.*
- [x] Object left / removed *Without a model: in **watched areas** (new zone type, drawn around a safe, a shelf or a corridor), the box keeps a slow and a fast background of the picture; a change that stays still for `min_static_s` (2 min by default) is an object left (it adds edges) or removed (it takes them away). Someone walking past doesn't count or end an event; a sudden change over much of the picture (lights) restarts the backgrounds; after `max_hold_s` (2 h) a change is the new normal. People standing still are left out once a person model runs; until then, watched areas must be where people don't stay. Tested on synthetic pictures and a recorded H.264 test video through the replay tool; not yet on footage from the branches.*
- POS integration (drawer vs sales), HRM integration (attendance → payroll)
- Model retraining pipeline that uses reviewed "false alarm" labels automatically
- Mobile app

---

## 5. Timeline at a glance

```
Week:            1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20
0 Discovery      ██ ██
1 Foundations       ██ ██
2 Edge core            ██ ██ ██ ██
3 Cloud core              ██ ██ ██ ██
4 Dashboard                     ██ ██ ██ ██
5 AI wave 1               ██ ██ ██ ██ ██ ██ ██ ██
6 Alerts/reports                         ██ ██ ██ ██
7 Security             ░░ ░░ ░░ ░░ ░░ ░░ ░░ ░░ ░░ ██ ██
8 Ops-system embed                                  ██ ██ ██
9 Install/shadow                                       ██ ██ ██
10 30-day pilot                                              ██ ██ ██ ██ ██
11 Handover                                                              ██ ██
```

Milestones: **M1** end of Week 2 (scope + data) · **M2** Week 6 (end-to-end event on simulated cameras) · **M3** Week 9 (dashboard usable) · **M4** Week 12 (wave 1 meets offline targets) · **M5** Week 14 (pilot starts) · **M6** Week 18+ (pilot sign-off) · **M7** Week 20 (handover).

---

## 6. Retention defaults (recommendation)

| Data | Where | Default | Notes |
|---|---|---|---|
| Continuous footage | Existing NVR | 30 days | Limited by NVR disk. **Check Kuwait Law 61/2015 minimum.** |
| Event clips + snapshots | Edge (cache) | 7 days | Upload buffer |
| Event clips + snapshots | Cloud | 90 days | Set per tenant (30/60/90/180) |
| Event records (metadata) | Cloud DB | 1 year | Small. Needed for trends. |
| Daily/weekly reports | Cloud | 2 years | |
| Face templates | Cloud + edge (encrypted) | While employed | Deleted when the staff member leaves |
| Audit logs | Cloud | 1 year+ | |

---

## 7. Bandwidth estimate (per branch)

- Events: about 50–300 per day × (2–5 MB clip + 100 KB snapshot) ≈ **0.2–1.5 GB/day**. This is small.
- Live view (on demand): sub-stream ≈ 0.5–1 Mbps per camera viewed. A 4-camera grid ≈ 4 Mbps upload.
- NVR playback (on demand): main stream ≈ 2–4 Mbps per camera.
- → The brief's **10 Mbps upload** is enough for events plus a few live views at the same time.

---

## 8. Definition of done (every feature)

1. Can be turned on/off per tenant/branch/camera, and inference does not run when it's off
2. Saves a clip + snapshot for every event
3. Events are searchable by all standard filters
4. Has an offline evaluation report on the test set
5. Has dedup/cooldown rules (no repeat alerts)
6. Appears in the daily report
7. Has unit tests for the rules + an integration test using a recorded footage sample
8. Is documented (what it detects, its parameters, known limits)

---

## 9. Main risks

| Risk | Impact | Mitigation |
|---|---|---|
| Gloves/hair cap hard to see from ceiling fisheye cams | Misses the 90% target | Agree on a camera re-position or extra kitchen cam; report "unknown" instead of guessing; adjusted target for gloves |
| Face recognition through the existing cameras | Bad attendance data | Dedicated entrance camera or face terminal (already in the brief) |
| Too little labelled data | Weak models | Start collecting in Week 1; label continuously; use reviewed events as new labels |
| Model licensing (AGPL / non-commercial weights) | Legal problem at rollout | Decide in Phase 0 |
| NVR issues (disk failure, wrong clock, old firmware) | Missing playback, wrong timestamps | Site-hygiene checklist in onboarding; edge monitors NVR health through ISAPI |
| Internet outages | Late alerts | Local outbox; critical alerts could also go out over a 4G backup modem |
| WhatsApp template approval delay | No WhatsApp alerts at pilot start | Apply in Phase 0/1 |
| Mudawara ops-system integration unknown | Delay in Phase 8 | Get their tech contact in Phase 0 |
| Scope creep (all 8 features at once) | Tuning never finishes | Stick to the brief's own advice: 4 features first, then counting/queue |

---

## 10. Next actions (this week)

1. Send the §1 question list to Ibrahim and book the kickoff call (the brief asks for one this week).
2. Tell the owner about the **Egaila NVR "Disk Exception"** and the **wrong NVR clocks**.
3. Schedule the site survey; ask for LAN access and a read-only NVR account.
4. Start exporting sample footage from the kitchen, counter and entrance cameras.
5. Get quotes for the Jetson Orin units and a staff-entrance face camera/terminal.
6. Set up the monorepo skeleton and the simulated-camera dev environment.
