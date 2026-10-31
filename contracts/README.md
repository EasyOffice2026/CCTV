# cctv-contracts — edge ↔ cloud API (v1)

Pydantic models shared by the edge service (Python 3.10, Jetson) and the cloud API (Python 3.12).
JSON Schemas for other languages are in [`schemas/v1/`](schemas/v1/) (regenerate with
`python -m cctv_contracts.export`; a test fails if they are stale).

## Endpoints

All under `/v1/edge`, authenticated with the device token (`Authorization: Bearer …`),
except `/pair`, which is how a box gets its token.
The token identifies the tenant and branch, so they are never sent in request bodies.

| Method & path | Body → response | Purpose |
|---|---|---|
| `POST /pair` | `PairRequest` → `PairResponse` | Once per new box, **no token**: swap the one-time pairing code for the device token |
| `POST /media/upload-urls` | `UploadUrlRequest` → `UploadUrlResponse` | Get a pre-signed URL, then `PUT` the snapshot/clip there |
| `POST /events` | `EventBatch` → `EventBatchResponse` | Send up to 100 events; per-event accept/reject |
| `POST /heartbeat` | `Heartbeat` → `HeartbeatResponse` | Every 30 s: camera health, system stats, outbox size |
| `GET /config` | → `EdgeConfig` (or `304`) | Resolved config; `If-None-Match: <version>` |
| `GET /remote?wait=25` | → `RemoteRequest` (or `204`) | Long poll for requests only the box can serve: live view and playback (WebRTC offer → answer via go2rtc), the NVR's recordings, a camera frame, face embeddings |
| `POST /remote/{id}/reply` | `RemoteReply` → `204` | Answer a request once; `404` if it expired or was sent to another box |

## Event flow

```
detection starts ─► save snapshot ─► upload-urls + PUT ─► POST /events  (status=open, snapshot_key)
detection ends   ─► cut clip (pre/post roll) ─► upload-urls + PUT ─► POST /events  (same event_id,
                                                                     status=closed, ended_at, clip_key)
```

- `event_id` is a UUID generated on the edge and is the **idempotency key**. Retries and the
  open → closed update reuse it, so the cloud never creates duplicates and alerts fire once.
- Events wait in the edge outbox until the cloud accepts them, so nothing is lost while the
  internet is down.
- Every timestamp must include a UTC offset and is stored in UTC. The edge clock is NTP-synced;
  never read time from the NVR's on-screen overlay.

## Rules enforced by the models

- `event_type` must belong to `feature` (`EVENT_FEATURE` in `enums.py`).
- Every event has a snapshot; a closed event also needs `ended_at` and a clip (brief: "every
  detection must save a short video clip and snapshot").
- RTSP URLs in config never contain credentials; the edge looks up `credentials_ref` locally.
- Zones use coordinates normalised to 0..1; `entrance_line` zones are 2-point lines, other zones
  are polygons with at least 3 points.
- The config lists only **enabled** features per camera (camera > branch > tenant > default),
  so the edge runs exactly those and nothing else.

## Versioning

Inside v1, changes must be additive (new optional fields, new enum values). Unknown fields are
ignored on both sides. Anything else is a breaking change and gets a new `/v2` prefix and a new
`cctv_contracts.v2` package, with v1 kept until every edge device is upgraded.
