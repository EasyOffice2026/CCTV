# cctv-edge: edge AI service

Runs on the Jetson box in each branch. It reads the cameras, detects and tracks people,
applies the rules for the features that are switched on, saves a snapshot and clip for every
event, recognises enrolled staff for attendance, counts people, and sends everything to the
cloud. When the internet is down, nothing is lost.

## How it works

```
camera main stream ─► ring buffer (2 s segments, last 4 min, no re-encode) ──────┐
camera sub-stream ─► decode 5 fps ─► detectors ─► tracker ─► zones ─► rules ─────┤
                         │                              ├─► PPE / drawer classifiers ┤
                         │                              └─► counts (5-min buckets) ──┤
                         └─► faces ─► match enrolled staff ─► sightings ─────────────┤
                                                                                 ▼
                     event opened ─► snapshot.jpg ─► outbox (SQLite) ─► sync ─► cloud
                     event ended  ─► wait post-roll ─► clip.mp4 from ring buffer ─► outbox
```

| Module | Does |
|---|---|
| `streams.py` | ffmpeg decode of the analysis stream, reconnect with backoff, health (online / degraded / offline) |
| `ringbuffer.py` | Keeps recent main-stream footage so clips include the 10 s **before** an event |
| `detection.py` | Detector interface, motion detector |
| `models/` | Model manifests, ONNX detectors (YOLOX, RT-DETR/D-FINE) and crop classifiers, registry |
| `tracking.py` | ByteTrack-style tracker (two-stage matching, velocity prediction) |
| `pipeline.py` | Per camera: detect → ignore zones → track → zone tests → PPE / drawer / counts → rules; faces |
| `rules.py` | Open/close events: schedule, min duration, max gap, cooldown, presence or absence, per-feature rules and params |
| `ppe.py` | Kitchen compliance: quality gate, mask / gloves / hair cap verdicts per person |
| `counts.py` | Entrance-line in/out and zone occupancy per 5-minute bucket |
| `face.py`, `attendance.py` | Face analyzer, enrolment embeddings, in-memory staff templates, sightings |
| `media.py` | JPEG snapshot with boxes; H.264 MP4 clip (browser-playable) |
| `outbox.py` | Every event and file is stored locally first; disk quota with oldest-first cleanup |
| `sync.py`, `cloud.py` | Upload media to pre-signed URLs, send events in batches, retry with backoff |
| `service.py` | Applies cloud config without restart; clip, sync, config, heartbeat, remote loops |
| `remote.py`, `go2rtc.py`, `nvr.py` | Requests from the cloud: live view, NVR playback and recordings, frames, face enrolment |
| `replay.py` | Offline evaluation: the pipeline over a video file |

Only features enabled in the cloud config run. A camera with no enabled feature is not
streamed or analysed at all.

## Models

No model weights are in git: they are licensed separately (plan §1 #12) and copied onto
the box at install time (`MODELS_SOURCE=... provision.sh`, into `/opt/cctv-edge/models`,
mounted read-only). Without models the box runs motion detection only; features whose
model is missing are logged at startup and simply don't fire.

Each model is a folder with its weights and a `model.json`:

```json
{
  "name": "person-yolox-s",
  "version": "2024-05",
  "task": "detector",
  "file": "model.onnx",
  "sha256": "<sha256 of model.onnx>",
  "licence": "Apache-2.0",
  "source": "https://github.com/Megvii-BaseDetection/YOLOX (yolox_s, COCO)",
  "input": {"width": 640, "height": 640, "scale": 1.0, "channel_order": "bgr", "letterbox": true},
  "output_format": "yolox",
  "classes": ["person", "bicycle", "car", "...", "cell phone", "..."],
  "label_map": {"person": "person", "cell phone": "phone"},
  "score_threshold": 0.4,
  "nms_iou": 0.5,
  "options": {"strides": [8, 16, 32], "decoded": false}
}
```

- `licence` is required and `sha256` is checked before loading: a model with an unknown
  licence or changed weights is refused.
- `task`: `detector`, `drawer_classifier` (classes `closed`, `open`, `softmax`),
  `ppe_classifier` (classes `mask`, `gloves`, `hair_cap` = probability worn, `sigmoid`),
  `face_detector` and `face_embedder` (`"runtime": "opencv"`).
- Detector `output_format`: `yolox` (raw, or `"decoded": true` if the export includes the
  decode) or `rtdetr` (RT-DETR / D-FINE exports: either labels/boxes/scores with an
  `orig_target_sizes` input, or boxes + logits). Use `"letterbox": false` for these.
- `input` sets resizing and normalisation: pixel × `scale`, then `(x - mean) / std`.
- Detectors and classifiers run on ONNX Runtime (`pip install cctv-edge[models]`; on the
  Jetson NVIDIA's `onnxruntime-gpu` for JetPack). `CCTV_EDGE_MODEL_PROVIDERS` lists the
  providers in order (TensorRT, CUDA, CPU by default); missing ones are skipped.
- Each model is loaded once and shared by all cameras; applying a new config does not
  reload it.

Licence-safe candidates (to confirm with the client): YOLOX, RT-DETR and D-FINE
(Apache-2.0) for people; YuNet (MIT) and SFace (Apache-2.0) from the OpenCV model zoo for
faces. The drawer and PPE classifiers are trained on Mudawara footage (see `ml/`).

## Features and their rules

Only enabled features run, and only their models. Every rule can be tuned per tenant,
branch or camera in the feature's `params` with the event type as key, e.g.
`{"after_hours_person": {"min_duration_s": 5, "cooldown_s": 600}, "after_hours_motion": {"enabled": false}}`.
Rule keys: `enabled`, `min_duration_s`, `max_gap_s`, `cooldown_s`, `min_confidence`, `min_count`,
`severity`. Feature-wide settings sit next to them (below). Unknown keys are logged and
ignored.

### Security (after hours, restricted areas)

| Event | Fires when | Defaults |
|---|---|---|
| `after_hours_person` | a person is visible outside working hours | 3 s min, 5 min cooldown per zone, critical |
| `after_hours_motion` | motion outside working hours | 2 s min, 10 min cooldown |
| `restricted_area_entry` | a person stands in a `restricted` zone | 2 s min, 2 min cooldown, critical |

Needs a person detector (motion works without). With no working hours configured the
branch counts as always open, so after-hours rules stay silent. Draw `ignore` zones over
TV screens, reflections and the street seen through windows.

### Cashier

| Event | Fires when | Defaults |
|---|---|---|
| `counter_unattended` | nobody stands in a `counter` zone during working hours | 60 s min, ends when someone is back for 2 s, 5 min cooldown |
| `drawer_open` | the drawer classifier says `open` for a `cash_drawer` zone | 1 s min, 5 s cooldown (every opening is logged), info |

Settings: `drawer_open_threshold` (0.6). The drawer zone should be drawn tightly around the
drawer, seen from above. Counter occupancy (most people at once, seconds staffed) is sent
as 5-minute count buckets for reports. Known limits: matching drawer openings with POS
sales needs the POS integration (later phase).

### Kitchen compliance (mask, gloves, hair cap)

People standing in a `kitchen` zone are cropped and checked by the PPE classifier, at most
once per `classify_interval_s`. An item is **missing** only when the classifier's
probability is at or below `missing_threshold`, **worn** at or above `worn_threshold`, and
**unknown** in between or when the crop is too small or blurred. Unknown never becomes a
violation.

| Event | Defaults |
|---|---|
| `missing_mask`, `missing_gloves`, `missing_hair_cap` | missing for 5 s, then one event per person per 15 min (cooldown per person, not per zone) |

Settings and defaults: `min_crop_height_px` 96, `min_sharpness` 20 (variance of the
Laplacian), `worn_threshold` 0.6, `missing_threshold` 0.3, `classify_interval_s` 1.
Which items are required is set by switching rules off, e.g. `{"missing_mask": {"enabled":
false}}`; events carry `missing` and `checked_items` in their attributes. Kitchen cameras
are analysed at 1280×720 (`CCTV_EDGE_DETAIL_WIDTH/HEIGHT`), so their sub-stream should be
at least 720p (or have none, so the main stream is used). Known limits: gloves seen from a
ceiling fisheye are hard (plan §9); a person who leaves view and returns gets a new track
and can be reported again.

### Attendance (face recognition)

On cameras with the attendance feature (the staff entrance), faces are detected, checked
for quality, embedded and compared with the enrolled staff templates of the branch:

- A match at or above `match_threshold` (cosine similarity) becomes a **sighting** (staff,
  camera, time), at most one per staff member and camera per `sighting_interval_s`. The
  cloud makes the first sighting of a work day the check-in and the last the check-out.
- **A face that matches nobody is dropped immediately**: not stored, not logged, not sent.
- Templates come from `GET /v1/edge/face-templates` (ETag), are held **in memory only**,
  and are refetched every 5 minutes and whenever the config version changes (the cloud
  bumps it on enrolment, deletion and consent changes). Only templates made with the
  box's own face model version are used. No templates: faces aren't even looked at.
- Enrolment photos from the dashboard are embedded in memory (never written to disk):
  each photo must show one clear face (the largest face is used only if clearly the
  largest), and the photos must look like the same person.

Settings and defaults: `match_threshold` 0.40 (SFace's published threshold is 0.363; a bit
stricter means fewer wrong matches, check on site), `min_face_px` 60, `min_sharpness` 30,
`min_detector_score` 0.85, `sighting_interval_s` 60. Needs the `face` extra (OpenCV) and a
`face_detector` + `face_embedder` model. Option A of the plan, a Hikvision face terminal
reporting through ISAPI, is a later add-on that would feed the same sightings.

### Customer count

With the feature on, people crossing `entrance_line` zones are counted in and out per
5-minute bucket (`POST /v1/edge/counts`), and the most people at once in `dining` zones.

| Event | Fires when | Defaults |
|---|---|---|
| `occupancy_high` | a `dining` zone holds at least `min_count` people | 30 people, 2 min, 30 min cooldown, info |

**Staff are left out** (plan 10.2): a track seen in a `staff_area`, `kitchen` or `counter`
zone of the same camera is staff from then on (forgotten 10 minutes after it was last
seen) and isn't counted at entrance lines or in dining and queue zones; crossings left out
are sent as `staff_crossings`. Staff never seen in such a zone are still counted.

### Queue

With the feature on, `queue` zones report the most people at once and how long each
person's track stayed in the zone (a wait ends 3 s after leaving; under 5 s is passing
through): `waits`, `wait_total_seconds`, `wait_max_seconds` per bucket. A lost and re-found
track splits a wait, so waits are on the low side.

| Event | Fires when | Defaults |
|---|---|---|
| `queue_long` | a `queue` zone holds at least `min_count` people | 5 people, 60 s, 10 min cooldown |

Events of rules with `min_count` carry `people` (how many were in the zone when it opened).

### Objects left or removed

In **watched areas** (`watch_area` zones), changes that stand still (`cctv_edge/objects.py`),
without a model: a slow background (what the area normally looks like) and a fast one (what
it looks like now) on a small grey picture, updated once a second. A spot that differs from
the slow one and has stopped changing for `min_static_s` is an object left (it adds edges,
like a bag) or removed (it takes them away, like a missing box); `object_left` (warning)
and `object_removed` (critical) events last until it is put back or taken away.

| Setting | Default | |
|---|---|---|
| `min_static_s` | 120 | How long a change must stand still |
| `max_hold_s` | 7200 | After this long, the change is the new normal |
| `absorb_s` | 600 | How fast the slow background follows gradual change (daylight) |
| `difference` | 30 | Grey levels (0-255) that count as a change |
| `min_area` | 0.003 | Smallest change, as a share of the picture |

Someone walking past doesn't count or end an event. A change over 40% of the picture at
once (lights switched, camera moved) restarts the backgrounds. People tracked by a person
model are left out; without one, someone sitting still for two minutes looks like an
object, so draw watched areas where people don't stay (a safe, a stock shelf, an emergency
exit corridor), not over tables.

### Heatmap

Where people stand (`cctv_edge/heatmap.py`), for cameras with the Heatmap feature: every
frame, each person's foot point adds the time since the last frame to its cell of a 32 x 18
grid over the picture. Staff (seen in a staff area, kitchen or counter zone of the camera)
are left out, as in customer counts, and so are detections in ignore zones. One bucket per
hour, sent like counts (`POST /v1/edge/heatmaps`); only totals leave the box. Gaps between
frames over 2 s (a stream drop) add nothing.

## Live view, playback and other requests from the cloud

The dashboard sometimes needs something only the box can reach on the branch LAN. The box
long-polls `GET /v1/edge/remote` (`remote.py`) and answers each request once:

| Request | What the box does |
|---|---|
| `live` | Adds the camera's sub-stream to **go2rtc** as `live-<camera id>` and returns go2rtc's WebRTC answer to the browser's offer |
| `playback` | Same, from the NVR recording (`/Streaming/tracks/<ch>01?starttime=…`) as `pb-<request id>`; removed after 3 h |
| `recordings` | Lists recorded spans from the NVR (Hikvision ISAPI `ContentMgmt/search`, digest auth) |
| `frame` | One JPEG from the sub-stream (ffmpeg), e.g. for drawing zones |
| `face_embedding` | Embeds 1–5 enrolment photos in memory with the face model; refused when none is installed |

Video goes from go2rtc straight to the browser; the cloud only passes the SDP offer and
answer. go2rtc (`infra/edge/go2rtc.yaml`) keeps its API on `127.0.0.1:1984`. Browsers
connect to **port 8555 (TCP and UDP)**, so allow it in the branch firewall, or give both
sides a TURN server for networks where a direct connection fails (most mobile networks).
Camera credentials go to go2rtc only through its local API; its config is mounted
read-only, so they are never written to disk. Errors sent back to the cloud never
contain credentials.

Hikvision NVRs read the times in playback URLs and searches as their **local** time,
even though they end in `Z`. The box converts using the branch time zone, so the NVR's
clock and time zone must be right (NTP, Asia/Kuwait).

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `CCTV_EDGE_CLOUD_URL` | `http://localhost:8000` | Must be `https://` except to this machine |
| `CCTV_EDGE_ALLOW_INSECURE_CLOUD` | `false` | Allow plain http to another machine (dev only) |
| `CCTV_EDGE_DEVICE_TOKEN` | (required) | From pairing; identifies the tenant and branch. Rotated tokens are saved in `<data dir>/device_token` |
| `CCTV_EDGE_DATA_DIR` | `/var/lib/cctv-edge` | Outbox, media, ring buffer, last config |
| `CCTV_EDGE_VERSION` | the package version | The release this image is, sent with heartbeats; set by the release workflow (software updates) |
| `CCTV_EDGE_CREDENTIALS_FILE` | `/etc/cctv-edge/credentials.json` | NVR/camera passwords |
| `CCTV_EDGE_FFMPEG` | `ffmpeg` | |
| `CCTV_EDGE_ANALYSIS_FPS` | `5` | |
| `CCTV_EDGE_DISK_QUOTA_BYTES` | 20 GiB | Local media limit |
| `CCTV_EDGE_DETECTORS` | `["motion"]` | Built-in detectors |
| `CCTV_EDGE_MODELS_DIR` | `<data dir>/models` | `/models` in the container |
| `CCTV_EDGE_MODEL_PROVIDERS` | TensorRT, CUDA, CPU | ONNX Runtime providers in order |
| `CCTV_EDGE_DETAIL_WIDTH` / `_HEIGHT` | 1280 / 720 | Analysis size for kitchen and attendance cameras |
| `CCTV_EDGE_FACE_TEMPLATES_SECONDS` | `300` | Face template refresh |
| `CCTV_EDGE_GO2RTC_URL` | `http://127.0.0.1:1984` | Local go2rtc API |
| `CCTV_EDGE_STUN_URL` | Google public STUN | Used by go2rtc (compose) |

See `cctv_edge/settings.py` for the rest.

Camera passwords never come from the cloud. The config names a `credentials_ref`, and the
box looks it up in the credentials file:

```json
{"nvr-main": {"username": "ai-reader", "password": "..."}}
```

Use a read-only NVR user, created for this purpose. The file must be readable by the
service user only (`chmod 600`); the service warns at start-up when it isn't.

**Device token.** The cloud asks for a new token every 30 days (in the heartbeat reply).
The box saves it to `<data dir>/device_token` (mode 600, with a hash of the paired token
it replaced) before using it, and loads it at start-up. If the box is paired again (a new
`CCTV_EDGE_DEVICE_TOKEN`), the saved file no longer matches and is ignored.

## Install on a Jetson

```bash
sudo NVME_DEVICE=/dev/nvme0n1 FORMAT_NVME=yes TAILSCALE_AUTHKEY=tskey-... infra/edge/provision.sh
```

This installs Docker and NTP, mounts the NVMe drive, sets up the hardware watchdog, the VPN,
the boot service and the software updater, then starts the container from `infra/edge/docker-compose.yml`. The
service exits if one of its loops hangs, and Docker restarts it.

Add `ENCRYPT_NVME=yes` (with `FORMAT_NVME=yes`, on a new drive) to encrypt the drive with
LUKS2 before formatting it (plan 7.2). The key is kept on the internal eMMC, so a removed or
discarded NVMe can't be read; protecting a stolen box as a whole needs Jetson disk
encryption at flash time (see [docs/security.md](../docs/security.md)).

## Software updates

Releases reach the boxes from the dashboard (Administration › Edge software, super-admins):
the cloud names the release with the heartbeat response, the service writes it to
`<data dir>/update/request.json`, and the updater (`cctv_edge/updater.py`, installed to
`/opt/cctv-edge/updater.py` and run every minute as root by `cctv-edge-update.timer`) does
the switch: pull by digest, check the repository and the cosign signature of our release
workflow, set `CCTV_EDGE_IMAGE` in edge.env, `docker compose up -d edge`, and wait for the
new version's heartbeat; no heartbeat at least 2 minutes after the switch and within 10, or
more than 2 restarts, and it switches back. Its report (`status.json`) goes back with the
heartbeats. The updater uses the standard library only, because it runs on the box's own
Python. The image reports the release it was built as in `CCTV_EDGE_VERSION` (the
release workflow's `VERSION` build argument).

In edge.env: `CCTV_EDGE_UPDATE_SIGNER` (cosign certificate identity, a regexp; default:
`release.yml` of this repository on a `v*` tag), `CCTV_EDGE_UPDATE_REPOSITORY` (default: the
repository of the current image), `CCTV_EDGE_COSIGN_IMAGE`, and, on lab boxes only,
`CCTV_EDGE_UPDATE_UNSIGNED=yes`. Runbook: [docs/runbooks/edge-recovery.md](../docs/runbooks/edge-recovery.md#update-the-edge-software).

## Site check (install day)

```bash
sudo docker compose -f /opt/cctv-edge/docker-compose.yml exec edge python -m cctv_edge --check [--json]
```

One line per finding (OK / WARN / FAIL); exits with 1 on a failure. It checks the device
token (by fetching the config), the cloud and the box's clock against it, disk and
temperature; every camera's main and sub-stream with `ffprobe` (codec H.264/H.265, main
stream 1080p and 15 fps or better, credentials from `credentials.json`, never printed);
and each NVR through ISAPI (GET only): clock against the box, time zone against the
branch's, NTP or manual, and disk states (anything but ok/idle fails: "Disk Exception"
means nothing is recorded). In the container it can't see NTP; run `timedatectl` on the
host. See [docs/runbooks/install.md](../docs/runbooks/install.md).

Heartbeats include each camera's `stream_restarts` (times the analysis stream ended and
was reopened since the service started), which the cloud's 48-hour setup test uses.

## Offline evaluation (replay)

Runs a camera's real pipeline over recorded footage, for the per-feature evaluation on
the held-out test set (plan 5.5):

```bash
python -m cctv_edge.replay kitchen-2026-11-02.mp4 --camera camera.json \
    --start 2026-11-02T21:00:00+03:00 --hours hours.json --models-dir models/ \
    --out events.jsonl --coverage coverage.csv
```

`camera.json` is a `CameraConfig` (features, params, zones), `hours.json` a list of
`WorkingHours`, `--start` the time of the first frame. Each closed event is appended to
`events.jsonl` as one line with `event_id`, `camera_id`, `feature`, `event_type`,
`zone_id`, `track_id`, `started_at`, `ended_at` (ISO-8601 UTC), `confidence` and
`attributes`. `--coverage` appends `camera_id,started_at,ended_at` for the replayed span.
The scoring tools in `ml/` read both. Attendance isn't replayed.

## Development

```bash
pip install -e contracts -e "edge[test,models,face]"
cd edge && pytest
```

The tests use a fake cloud, scripted detections and fake classifiers / face analyzers, so
they need no weights. Tests that need ffmpeg use the one on `PATH`, or the binary from
`imageio-ffmpeg` (installed with the `test` extra). The one test that runs a real (tiny)
ONNX model is skipped without onnxruntime.

Running the service against the local stack needs the cloud API (`/v1/edge`). Live view
also needs go2rtc (see `infra/edge/docker-compose.yml`).
