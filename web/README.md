# cctv-web: dashboard and admin panel

Next.js 16 + React 19 + TypeScript, TanStack Query, Tailwind CSS 4. English and Arabic
(right-to-left). Built as a **static single-page app** (`output: "export"`): no Node server
in production; nginx serves the files and proxies `/v1` to the API (see `Dockerfile`).

## Run it

```bash
cd web
cp .env.example .env.local     # NEXT_PUBLIC_API_URL=http://localhost:8000
npm ci
npm run dev                     # http://localhost:3000 (the API must allow this origin: CCTV_WEB_URL)
```

With the Docker stack (`infra/docker-compose.yml`) the dashboard is on http://localhost:3000
and talks to the API through its own proxy.

| Command | |
|---|---|
| `npm run dev` | Development server |
| `npm run build` | Static export to `out/` (includes the TypeScript check) |
| `npm run lint` / `npm test` | ESLint / unit tests (Vitest: time zones, zone geometry, translations) |
| `npm run api:types` | Regenerate `src/api/schema.d.ts` from `cloud/openapi.json` (after `python -m cctv_cloud.openapi`); CI fails if it is stale |

## Pages

| Page | Who (permission) | What |
|---|---|---|
| Overview | `kpis_view` | Each branch today: events, issues, attendance, customers, cameras and boxes, alert times, latest events, reports; sections follow the feature switches; refreshes every minute. Home page of owners and managers |
| Onboarding | `devices_manage` | The branch's 11 set-up steps from survey to go-live, worked out from what exists, the next one highlighted, each linked to its page (plan 11.1) |
| Edge software | `platform_admin` | Edge software releases (from the release workflow's summary) and their rollout to one box, a client or everyone, with each box's version and last update (updating, updated, failed, rolled back) (plan 2.10) |
| System health | `platform_admin` | Every client's open incidents (boxes offline, streams down, disks, temperature, uploads) for on-call (plan 11.4); each branch's own are on the Edge devices page, with the box's disk, temperature and upload backlog |
| Pilot | `kpis_view` | The official pilot of the chosen branch: pilot day, the success criteria with met / not met, precision per feature, a table per day (false alarms, alert p95, camera and box uptime), and the pilot log (owners and our team write; super-admins set day 1) |
| Daily review | `events_review` | The day's unreviewed events of the chosen branch, oldest first, with clip; keys V valid, F false alarm, R reviewed, S skip, B back. Below: review results of the last 7 days per event type, camera and zone (precision, false alarms/day) for tuning (plan 9.4, 9.5) |
| Live events | `events_view` | Event cards that update live over the WebSocket; clip player; Valid / False alarm / Reviewed |
| Attendance: payroll | `payroll_export` (owners) | A month of attendance as CSV for the payroll system, on the Attendance page |
| My account › Phone app | everyone | Install the dashboard as an app (PWA: `public/manifest.json`, `public/sw.js`, `src/lib/install.ts`; icons from `scripts/make_icons.py`): an install button where the browser offers it, the steps for iPhone otherwise. The service worker also shows `public/offline.html` without a connection and caches only `_next/static` files and icons, never API answers or media |
| Heatmap | `events_view` | Where customers stood in a camera's view over a period, on a current camera picture (with `live_view` or `devices_manage`), and the busiest areas of the picture as a list |
| Search | `events_view` | Every filter of the search API; CSV export (`media_export`) |
| Live view | `live_view` | Camera grid per branch, WebRTC from the edge box on demand; stops after 5 min without activity or when the tab is hidden |
| Playback | `live_view` | A day's timeline of the NVR's recordings with event markers; click to play |
| Zones | `devices_manage` edits, `settings_manage` views | Draw polygons and count lines on a live frame |
| Features | `settings_manage` | On/off per tenant, branch and camera; thresholds (JSON) |
| Working hours | `branch_edit` | Weekly opening hours per branch |
| Staff | `staff_manage` | Staff, shifts, consent, face enrolment (only after consent) |
| Attendance | `staff_manage` | A day's check-in/out per person; correct and undo |
| Reports | `reports_view` | Daily, weekly and monthly branch reports; make or update one; PDF and Excel in the current language. `?report=<id>` opens one (links in report messages) |
| Alerts | `events_view` | The user's alert rules, WhatsApp number and consent, message language, report links, browser notifications, test messages |
| Audit log | `tenant_admin` | Who signed in, viewed or exported footage, reports and event lists, and changed settings, users, devices, staff or consent; filters, CSV export. Super-admins also see the platform log (their team's sign-ins) |
| Integrations | `tenant_admin` | API keys, webhooks (test, recent sends), the operational system's sign-in (super-admins set it up; owners see it) |
| Clients, Branches, Cameras & NVRs, Edge devices, Users | `platform_admin`, `tenant_admin`, `devices_manage` | Administration. Edge devices also shows the 48-hour setup test of the chosen branch (plan 9.2). Super-admins turn shadow mode on per branch (Branches) and mark the support team's accounts (Users); a banner shows shadow mode on every page |
| `/sso/` | — | Opened from the operational system with `#token=…&next=…`: signs in without a password |
| `/embed/overview/` | `kpis_view` | The overview for the operational system's iframe (`#token=…&lang=…&branch=…`, or a token by `postMessage`); no navigation. Only this path may be framed, by the origins in `EMBED_ANCESTORS` |

The UI hides what a role can't use; the API enforces it.

## How it works

- **Sign-in.** The access token lives in memory only. The refresh token is an HttpOnly,
  SameSite=Strict cookie set by the API (path `/v1/auth`), so scripts never see it. The app
  refreshes on load, on a 401, and shortly before expiry while the user is active (so the
  server's 8-hour idle timeout still applies).
- **Sign-in from the operational system.** `/sso/` and `/embed/` read a one-time token from
  the URL fragment (`src/lib/handoff.ts`), remove it from the address bar and swap it at
  `/v1/auth/sso`. In another site's frame there is no cookie, so the embedded page keeps
  its refresh token in memory; a reload needs a new token from the parent page.
- **Live events.** `/v1/ws`; the token is sent as the first message, again after each refresh.
  One socket per tab is shared by the event feed and the alert toasts (`src/lib/live-events.ts`).
- **Alerts.** Alerts routed to the user arrive on the same socket as toasts (critical ones
  stay until closed). With notifications turned on (Alerts page), `public/sw.js` shows them
  as system notifications when no dashboard tab is in front; clicking opens
  `/events/?event=<id>`, through sign-in if needed (the `next` link keeps the query).
- **Video.** The browser sends a WebRTC offer to the API, which relays it to the branch's edge
  box (go2rtc) and returns the answer. Video then flows from the box to the browser directly,
  or through the TURN server in `CCTV_WEBRTC_ICE_SERVERS` when a direct path isn't possible.
- **Times** are shown in the branch's time zone (Asia/Kuwait), not the viewer's.
- **Arabic.** `dir="rtl"` on `<html>`; layouts use logical properties (`ms-`, `pe-`, `start-`…).
  Every text is in `src/lib/messages/en.ts` and `ar.ts`; TypeScript and a test check that both
  have every key.

## Known issue

`next build` on **Windows** writes the router's prefetch files under the wrong names (a
Next.js 16.4 export bug with `\` paths), so prefetches 404 and navigation falls back to
normal loads. Builds on Linux (CI, the Docker image) are correct.
