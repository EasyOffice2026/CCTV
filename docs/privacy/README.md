# Privacy (plan 7.7)

What personal data the system handles, where it flows, how long it is kept, and the texts
the client needs: the [staff consent form](consent-form.md) and the [CCTV notice](signage.md).
The technical controls are in [../security.md](../security.md).

To confirm with the client's counsel before the pilot (plan §1 and §6): the legal basis for
staff face recognition in Kuwait, the minimum footage retention under Law 61/2015, and the
wording of the consent form and signage.

## Who is in the data

| People | Data | Why |
|---|---|---|
| Customers and visitors | Appear in footage, snapshots and clips of events; counted at entrances and in queues, and where they stood (heatmaps) - numbers only | Security, service quality (customer count, queues, layout), compliance evidence |
| Staff | Appear in footage; attendance from face recognition (consenting staff only); name, employee code, shift; compliance events (mask, gloves, hair cap) in the kitchen; attendance and compliance figures in reports | Attendance, food-safety compliance, security |
| Dashboard users (owners, managers, reviewers) | Email, name, role, phone (if they chose WhatsApp alerts), sign-ins and actions in the audit log | Operating the system; accountability |

The system does **not** identify customers, store faces of anyone who hasn't consented, read
lips, estimate age, gender or emotion, or send any image outside the dashboard (alerts
carry a link that needs a sign-in; WhatsApp and email get text only).

## Data flow

```
                Branch LAN (restaurant)                        Cloud (one region, private network)
 ┌──────────────────────────────────────────┐      ┌───────────────────────────────────────────────┐
 │  Cameras ──RTSP──► NVR (30 days footage) │      │                                               │
 │                       │                  │      │   Object storage (encrypted)                  │
 │                       ▼ RTSP (read-only) │      │   clips + snapshots of events, 90 days        │
 │   Edge box (NVMe, LUKS)                  │      │        ▲ pre-signed https, audited            │
 │   • 4 min ring buffer in memory/disk     │─────►│        │                                      │
 │   • detection → events (no raw video)    │https │   PostgreSQL (encrypted, row-level security)  │
 │   • clip + snapshot per event, 7 days    │      │   events 1 year, attendance 1 year,           │
 │   • face match vs enrolled staff only;   │      │   reports 2 years, audit log 2 years,         │
 │     unknown faces discarded at once      │      │   face templates (encrypted) while employed   │
 │   • camera passwords stay here           │      │        │                                      │
 └──────────────────────────────────────────┘      │        ▼                                      │
                                                   │   Dashboard (sign-in, 2FA, roles, Arabic/EN) │
     Support laptop ──Tailscale VPN──► edge box    │   Alerts: in-app, browser push, WhatsApp,    │
     (maintenance, logged on the box)              │   email - links only, never images            │
                                                   └───────────────────────────────────────────────┘
```

What leaves the branch: event records (what, where, when, confidence, which staff member if
recognised), one snapshot and one short clip per event (10 s before to 10 s after), attendance
sightings (staff id, camera, time), count buckets and heatmaps (numbers: people per
hour, seconds per area of the picture), camera health. Continuous
footage never leaves the NVR; live view and playback stream on demand from the box to the
viewer's browser and are logged.

The client's operational system, when connected (plan 8, `docs/integration/ops-system.md`),
receives branch totals (numbers only) and, by webhook, event records without images or
names (what, where, when, and a link that needs a sign-in); it sends us the email of the
user signing in. It is the client's own system, not our processor.

Processors: the cloud provider (hosting, region to be decided in plan §1 #11), Meta
(WhatsApp messages: the recipient's number and the alert text), the browser vendors' push
services (encrypted notification payloads), the email provider (to be chosen), Tailscale
(VPN coordination, no traffic content).

## Retention

| Data | Where | Kept | Then |
|---|---|---|---|
| Continuous footage | NVR at the branch | 30 days (disk size; **check Law 61/2015 minimum**) | Overwritten |
| Ring buffer | Edge box | 4 minutes | Overwritten |
| Event clips and snapshots | Edge box | 7 days after upload (upload buffer) | Deleted, oldest first |
| Event clips and snapshots | Cloud storage | 90 days (`retention_media_days`, per client: 30/60/90/180) | Deleted by the retention job; lifecycle rule as backstop |
| Event records (no media) | Cloud database | 1 year (`retention_event_days`) | Deleted with their count buckets and attendance days |
| Attendance days, counts, heatmaps | Cloud database | 1 year | Deleted |
| Reports (figures, incl. attendance per person) | Cloud database | 2 years (`CCTV_REPORT_RETENTION_DAYS`) | Deleted |
| Face templates (encrypted embeddings, no photos) | Cloud database; in memory on the box | While employed and consenting | Deleted at once when the person leaves or withdraws consent |
| Enrolment photos | Nowhere (processed in memory on the box) | — | — |
| Audit log | Cloud database | 2 years, never less than 1 | Deleted by `audit_purge` |
| User accounts | Cloud database | While the account exists | Deactivated accounts keep their audit entries |
| WhatsApp numbers, consent date | Cloud database | Until the user removes the number | Removed by the user on the Alerts page |

## Rights and requests

* **Staff** can see their own attendance through their manager, withdraw face-recognition
  consent at any time (the manager records it on the Staff page; the template is deleted
  immediately), and are not disadvantaged for refusing: attendance then comes from the
  existing manual process.
* **Access or deletion requests** (customers, staff): the owner searches events by camera
  and time on the dashboard; clips can be downloaded for the person (logged) or deleted
  early by the support team on request. Keep a log of requests (template to add with the
  client's counsel).
* **Who can see what**: see the role table in `cloud/README.md`. Reports with attendance per
  person are limited to owners and branch managers. Every view and download is in the
  audit log, which owners can inspect.

## Documents for the client

* [consent-form.md](consent-form.md): staff consent for face recognition (English and Arabic),
  to be signed before enrolment; the signing date is recorded on the Staff page.
* [signage.md](signage.md): the CCTV notice for entrances and the staff area (English and
  Arabic).
* The retention table above, to be adopted in the client's policy.
