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
├── edge/             # Jetson service: ingest, pipelines, rules, clipper, sync agent
│   ├── pipelines/    # one module per feature (attendance, kitchen, cashier, ...)
│   └── models/       # model configs (weights stored outside git)
├── cloud/
│   ├── api/          # FastAPI app
│   ├── workers/      # alerts, reports, retention, backup
│   └── migrations/
├── web/              # Next.js dashboard + admin panel
├── ml/               # dataset tools, labelling exports, training & evaluation scripts
├── infra/            # Terraform, docker-compose, edge provisioning
└── docs/             # this plan, API docs, onboarding checklist, runbooks
```

---

## 3. Core data model (first version)

```
tenants(id, name, plan, retention_footage_days=30, retention_event_days=90)
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

- [ ] 1.1 Monorepo, branching rules, CI (lint, type-check, tests, Docker builds), pre-commit.
- [ ] 1.2 Dev environment: `docker-compose` with Postgres, Redis, MinIO (local S3), MQTT, API, web.
- [ ] 1.3 **Simulated cameras:** loop the collected footage as RTSP streams (MediaMTX + ffmpeg) so all development works without being on site.
- [ ] 1.4 Cloud infrastructure (staging) with Terraform: Postgres, object storage, container hosting, secrets manager, TLS.
- [ ] 1.5 DB schema v1 (§3) + migrations + **RLS policies** + a test that proves tenant A cannot read tenant B.
- [ ] 1.6 Edge↔cloud contract: event JSON schema, clip upload flow (pre-signed URLs), heartbeat, config pull. Version it (`/v1`).

**Exit:** `docker compose up` runs the whole stack on a laptop with simulated cameras.

### Phase 2 — Edge core (Week 3–6)

- [ ] 2.1 Jetson provisioning script: JetPack, Docker, NVMe, VPN agent, auto-start, watchdog.
- [ ] 2.2 **Stream ingest:** go2rtc pulls each camera once; AI reads the sub-stream (or main stream at reduced fps for PPE); reconnect logic; per-camera health status.
- [ ] 2.3 **Ring buffer:** keep the last ~30 s per camera as video segments, so a clip can include **pre-event** footage (e.g. 10 s before + 10 s after).
- [ ] 2.4 **Base pipeline:** decode → person detection → tracking (track IDs) → zone tests (point-in-polygon on foot point) → line crossing.
- [ ] 2.5 **Rules engine:** turns detections into events using config — zone, schedule (working hours / after-hours), min duration, cooldown. **Deduplication:** one ongoing event per track/zone, with an "ended" update instead of repeats.
- [ ] 2.6 **Clipper:** event → MP4 clip (H.264 so browsers can play it) + JPEG snapshot with the bounding box drawn.
- [ ] 2.7 **Outbox & sync:** events stored in SQLite first, sent to the cloud with retry/backoff, so **nothing is lost when the internet is down**. Local disk quota with oldest-first cleanup.
- [ ] 2.8 **Config sync:** pull the resolved feature settings, zones and schedules; apply without restart; only run pipelines for enabled features.
- [ ] 2.9 Heartbeat + metrics (FPS per camera, GPU/CPU temp, disk, queue length).
- [ ] 2.10 OTA updates: pull a signed Docker image tag set from the cloud; roll back if health fails.

**Exit:** on the simulated cameras, an "after-hours person" event appears in the cloud DB with clip and snapshot in under 10 s, and is buffered and synced correctly when the network is cut.

### Phase 3 — Cloud platform core (Week 4–7)

- [ ] 3.1 Auth: login, JWT + refresh, **TOTP 2FA** (required for owner/admin), password reset, session timeout.
- [ ] 3.2 RBAC: roles from §1 #2, branch-level access, checks on every endpoint (plus RLS as a second safety layer).
- [ ] 3.3 Admin API: tenants, branches, cameras, edge devices (register with a one-time pairing token), users.
- [ ] 3.4 Settings API: feature on/off per tenant/branch/camera, zones (polygons), working hours, thresholds. Push changes to the edge immediately (MQTT/WebSocket notify → edge pulls).
- [ ] 3.5 Event ingest API: idempotent (`dedup_key`), validates tenant/device, stores media keys, publishes to Redis.
- [ ] 3.6 **Search API:** filter by date range, branch, camera, zone, event type, staff, review status; paginated; indexes on `(tenant_id, started_at)`, `(tenant_id, feature_key, started_at)`, `(tenant_id, staff_id, started_at)`.
- [ ] 3.7 Media access: short-lived pre-signed URLs; **every view/export written to `audit_logs`**.
- [ ] 3.8 Retention worker: delete events/media older than each tenant's policy; object-storage lifecycle rules as a backstop.
- [ ] 3.9 OpenAPI docs + API keys for future POS/HRM integrations.

**Exit:** API test suite green, including tenant-isolation and permission tests.

### Phase 4 — Web dashboard v1 (Week 6–9)

- [ ] 4.1 App shell: login + 2FA, branch switcher, role-aware navigation, English + **Arabic (RTL)** support (the site names are Arabic).
- [ ] 4.2 **Event feed:** live (WebSocket), snapshot cards, filters, a one-click clip player, buttons for **Valid / False alarm / Reviewed**.
- [ ] 4.3 **Search page:** all filters from 3.6, results grid, open clip, export.
- [ ] 4.4 **Live view:** grid per branch, WebRTC through the edge (sub-stream), opened on demand, auto-closes after inactivity.
- [ ] 4.5 **Playback:** timeline per camera fetched from the NVR (ISAPI/RTSP playback) through the edge, with event markers on the timeline.
- [ ] 4.6 **Settings:** draw/edit zone polygons and count lines on a camera snapshot; feature toggles per branch/camera; working hours; alert thresholds.
- [ ] 4.7 **Admin panel:** clients, branches, cameras, devices (online/offline, versions), users.
- [ ] 4.8 Staff management: add staff, upload/enrol face (only after the consent checkbox + date), shifts.

**Exit:** a non-developer can set up a branch, draw zones, toggle features and review events without touching code.

### Phase 5 — Pilot AI features, wave 1 (Week 5–12, alongside the work above)

For **each** feature follow the same loop: **label → train/configure → offline evaluation on held-out footage → shadow mode on site → tune → enable alerts.**

- [ ] **5.0 Labelling setup:** CVAT or Label Studio; labelling guide per feature; split footage into train/val/**test** (the test set is never trained on and is used for the 90% check).

- [ ] **5.1 After-hours security** *(easiest — build first)*
  - Person detection + tracking in "restricted" zones or anywhere outside working hours.
  - Ignore zones (TV screens, glass reflections, the street seen through windows).
  - Minimum dwell time (e.g. 3 s) to stop flicker false alarms; severity = critical → WhatsApp.
- [ ] **5.2 Cashier area**
  - Counter zone: "unattended counter" = no staff in the counter zone for more than N seconds during working hours.
  - Drawer-open: small classifier on the drawer ROI (open/closed) from top-down footage. Log each opening as an event; later match it against POS transactions ("drawer open without sale").
  - Counter activity: occupancy timeline for reports.
- [ ] **5.3 Kitchen compliance (mask / gloves / hair cap)**
  - Person detection → crop → **PPE classifier/detector trained on Mudawara footage** (central kitchen + branch kitchen).
  - Only judge people inside the kitchen zone, and only when the crop is large and sharp enough (otherwise "unknown", not a violation).
  - Violation must persist across several frames/seconds before it becomes an event; one event per person per period.
  - Set the hygiene rules with the client (which items are required in which zone).
- [ ] **5.4 Staff attendance (face recognition)**
  - Option A (recommended for the pilot): **Hikvision face-recognition terminal** at the staff entrance; pull its check-in events via ISAPI.
  - Option B: dedicated 4 MP camera + edge face pipeline (detect → quality check → embed → match against enrolled templates with a threshold).
  - Enrolment: 3–5 images per staff member under the site lighting; store **encrypted embeddings only**, no photos.
  - Rules: first seen = check-in, last seen = check-out; compare with shift → late/absent; manual correction screen for managers.
  - Non-matching faces are thrown away right away (privacy rule).
- [ ] 5.5 Per-feature evaluation report: precision, recall, false alarms per day on the test set, plus a confusion review of failures.

**Exit:** each wave-1 feature hits its offline target (or has an agreed adjusted target) before shadow mode.

### Phase 6 — Alerts & reports (Week 9–12)

- [ ] 6.1 Alert router: event → matching subscriptions (user, feature, severity, branch) → channels.
- [ ] 6.2 Browser notifications (Web Push + in-app toast) with snapshot and clip link.
- [ ] 6.3 **WhatsApp Cloud API**: templates (e.g. `after_hours_intrusion`) approved by Meta, a short link to the clip (login required), rate limit.
- [ ] 6.4 **No repeat alerts:** one alert per ongoing event plus a cooldown per camera/zone/feature; send an optional "resolved" message.
- [ ] 6.5 Per-user alert preferences page (features, severity, quiet hours, channels).
- [ ] 6.6 **Reports:** daily branch summary (attendance, compliance issues, customer count, peak hours, security events), weekly/monthly trends, staff scorecard (attendance %, compliance score). Scheduled generation, then PDF/Excel download and an email/WhatsApp link.
- [ ] 6.7 Measure alert latency (event start → notification delivered) and store it per alert. This proves the "< 10 s" target.

### Phase 7 — Security, privacy & compliance (Week 3–13, alongside the work above; finish by Week 13)

- [ ] 7.1 TLS everywhere; edge↔cloud with per-device credentials (mTLS or device tokens), rotated.
- [ ] 7.2 Encryption at rest: DB (managed encryption), object storage (SSE), edge NVMe (LUKS), face embeddings encrypted at the application level with KMS.
- [ ] 7.3 Audit log UI (who viewed/exported what, when) for owners and super-admin.
- [ ] 7.4 Secrets: no NVR or camera passwords in git or the DB in plain text (use a secrets manager).
- [ ] 7.5 Consent workflow: no face enrolment without recorded consent; delete a staff member's biometric data when they leave.
- [ ] 7.6 Pen-test checklist (OWASP Top 10, tenant-isolation tests, IDOR on clip URLs).
- [ ] 7.7 Privacy documentation: data-flow diagram, retention table, consent form, signage text.

### Phase 8 — Mudawara operational system integration (Week 11–13)

- [ ] 8.1 Agree on the method: **SSO (OIDC / signed JWT handoff)** + embed the dashboard pages (iframe with CSP `frame-ancestors`, or a micro-frontend).
- [ ] 8.2 "Owner overview" page built for embedding: attendance, compliance, customer count, peak hours, alerts, reports, clips.
- [ ] 8.3 The same feature toggles apply (it is the same backend).
- [ ] 8.4 Optional: REST endpoints/webhooks so the ops system can pull KPIs into its own screens.

### Phase 9 — On-site install & shadow mode (Week 12–14)

- [ ] 9.1 Install the edge box at the pilot branch (UPS, wired LAN, VPN up), connect all cameras, register in the admin panel.
- [ ] 9.2 **Setup test** (brief test #1): all cameras streaming, 48 h without dropped streams.
- [ ] 9.3 Draw real zones, set working hours, enrol staff (with consent).
- [ ] 9.4 **Shadow mode (1–2 weeks):** events are recorded but **alerts go only to us**. Review every event daily and mark valid/false.
- [ ] 9.5 Tuning loop: adjust thresholds, ignore zones, dwell times; retrain on the false alarms collected (hard-negative mining).
- [ ] 9.6 Train the owner and managers (review flow, settings, alerts).

### Phase 10 — Official 30-day pilot (Week 14–18+)

- [ ] 10.1 Turn on wave-1 alerts for the client: attendance, kitchen compliance, cashier, after-hours security.
- [ ] 10.2 **Wave 2 once wave 1 is stable (~week 2 of the pilot):**
  - **Customer count:** entrance line crossing (in/out), occupancy, busy hours. Exclude staff (staff-zone heuristics, uniform classifier, or face-matched staff).
  - **Queue detection:** people in the queue zone over time; wait time ≈ time in queue (tracking) or queue length ÷ service rate; alert above a threshold.
- [ ] 10.3 Daily KPI dashboard for the pilot itself: accuracy (from review labels), false alarms/day, alert latency, uptime.
- [ ] 10.4 Weekly check-in with the owner; keep a feedback log.
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

### Phase 11 — Productisation & handover (Week 18–20)

- [ ] 11.1 **Onboarding checklist** (survey → camera check → connect → zones → go live) as a document and as an admin-panel wizard.
- [ ] 11.2 **Hardware kits:**
  - Small (≤ 8 cams): Orin Nano Super / Orin NX 8GB, 512GB NVMe, UPS
  - Medium (9–16 cams): Orin NX 16GB, 1TB NVMe, UPS
  - Large (> 16 cams): 2 × medium, or AGX Orin
  - Plus a face camera/terminal per staff entrance
- [ ] 11.3 Minimum camera spec sheet (1080p @ 15 fps, H.264/H.265, RTSP/ONVIF) + compatibility list (Hikvision, Dahua, ...).
- [ ] 11.4 Monitoring & on-call: alerts when an edge device goes offline, a stream drops, a disk fills or the GPU overheats.
- [ ] 11.5 Docs: architecture, API reference, deployment runbook, edge recovery runbook, model retraining guide.
- [ ] 11.6 Source code handover + a recorded walkthrough.

### Later phase (after pilot)

- Heatmaps (track positions are already available → aggregate into a floor-plan grid)
- Object left / removed
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
