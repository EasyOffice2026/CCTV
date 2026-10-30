# cctv-edge: edge AI service

Runs on the Jetson box in each branch. It reads the cameras, detects and tracks people,
applies the rules for the features that are switched on, saves a snapshot and clip for every
event, and sends everything to the cloud. When the internet is down, nothing is lost.

## How it works

```
camera main stream ─► ring buffer (2 s segments, last 4 min, no re-encode) ──────┐
camera sub-stream ─► decode 5 fps ─► detectors ─► tracker ─► zones ─► rules ─────┤
                                                                                 ▼
                     event opened ─► snapshot.jpg ─► outbox (SQLite) ─► sync ─► cloud
                     event ended  ─► wait post-roll ─► clip.mp4 from ring buffer ─► outbox
```

| Module | Does |
|---|---|
| `streams.py` | ffmpeg decode of the analysis stream, reconnect with backoff, health (online / degraded / offline) |
| `ringbuffer.py` | Keeps recent main-stream footage so clips include the 10 s **before** an event |
| `detection.py` | Detector interface, motion detector, IoU tracker |
| `pipeline.py` | Per camera: detect → ignore zones → track → zone tests → line counting → rules |
| `rules.py` | Open/close events: schedule, min duration, max gap, cooldown, one event per track/zone |
| `media.py` | JPEG snapshot with boxes; H.264 MP4 clip (browser-playable) |
| `outbox.py` | Every event and file is stored locally first; disk quota with oldest-first cleanup |
| `sync.py`, `cloud.py` | Upload media to pre-signed URLs, send events in batches, retry with backoff |
| `service.py` | Applies cloud config without restart; clip, sync, config, heartbeat loops |

Only features enabled in the cloud config run. A camera with no enabled feature is not
streamed or analysed at all.

## Event rules (security feature)

| Event | Fires when | Defaults |
|---|---|---|
| `after_hours_person` | a person is visible outside working hours | 3 s min, 5 min cooldown, critical |
| `after_hours_motion` | motion outside working hours | 2 s min, 10 min cooldown |
| `restricted_area_entry` | a person stands in a `restricted` zone | 2 s min, 2 min cooldown, critical |

Tune per branch or camera in the feature's `params`, e.g.
`{"after_hours_person": {"min_duration_s": 5, "cooldown_s": 600}, "after_hours_motion": {"enabled": false}}`.
With no working hours configured the branch counts as always open, so after-hours rules
stay silent.

**Person detection** needs a model and comes in Phase 5 (licence decision pending, see
the plan §1 #12). Until then only the motion rule fires on real footage.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `CCTV_EDGE_CLOUD_URL` | `http://localhost:8000` | |
| `CCTV_EDGE_DEVICE_TOKEN` | (required) | Identifies the tenant and branch |
| `CCTV_EDGE_DATA_DIR` | `/var/lib/cctv-edge` | Outbox, media, ring buffer, last config |
| `CCTV_EDGE_CREDENTIALS_FILE` | `/etc/cctv-edge/credentials.json` | NVR/camera passwords |
| `CCTV_EDGE_FFMPEG` | `ffmpeg` | |
| `CCTV_EDGE_ANALYSIS_FPS` | `5` | |
| `CCTV_EDGE_DISK_QUOTA_BYTES` | 20 GiB | Local media limit |
| `CCTV_EDGE_DETECTORS` | `["motion"]` | |

See `cctv_edge/settings.py` for the rest.

Camera passwords never come from the cloud. The config names a `credentials_ref`, and the
box looks it up in the credentials file:

```json
{"nvr-main": {"username": "ai-reader", "password": "..."}}
```

Use a read-only NVR user, created for this purpose.

## Install on a Jetson

```bash
sudo NVME_DEVICE=/dev/nvme0n1 FORMAT_NVME=yes TAILSCALE_AUTHKEY=tskey-... infra/edge/provision.sh
```

This installs Docker and NTP, mounts the NVMe drive, sets up the hardware watchdog, the VPN
and the boot service, then starts the container from `infra/edge/docker-compose.yml`. The
service exits if one of its loops hangs, and Docker restarts it.

## Development

```bash
pip install -e contracts -e "edge[test]"
cd edge && pytest
```

The tests use a fake cloud. Tests that need ffmpeg use the one on `PATH`, or the binary
from `imageio-ffmpeg` (installed with the `test` extra).

Running the service against the local stack needs the cloud's `/v1/edge` endpoints, which
are built in Phase 3.
