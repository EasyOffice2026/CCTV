# Hardware: edge kits and camera requirements (plan 11.2, 11.3)

For sales and installers quoting a new branch. Prices change; get current quotes. The
figures below are sizing guidance from the design (plan §2, §7), to be confirmed with the
pilot's measured load on the Jetson before they go into a price list.

## 1. Edge kits (11.2)

One kit per branch. The box sits on the branch LAN next to the NVR, wired, on a UPS.

| Kit | Cameras analysed | Edge computer | Storage | Also |
|---|---|---|---|---|
| **Small** | up to 8 | NVIDIA Jetson Orin Nano Super 8 GB, or Orin NX 8 GB | 512 GB NVMe SSD | UPS |
| **Medium** | 9 to 16 | Jetson Orin NX 16 GB | 1 TB NVMe SSD | UPS |
| **Large** | more than 16 | 2 × Medium (cameras split between the boxes), or one Jetson AGX Orin 32/64 GB | 1 TB NVMe per box | UPS |

Every kit:

- **Carrier board and case** with passive or fan cooling, rated for 40 °C ambient
  (restaurant back offices are hot; the monitoring pages on-call at 85 °C).
- **UPS**: line-interactive, ~600 VA, at least 15 minutes for the box, the PoE switch and
  the NVR, so a power cut doesn't drop recording and events.
- **Network**: a wired port on the same LAN or VLAN as the NVR and cameras; outbound HTTPS
  to the cloud; upload of at least 10 Mbps (plan §7). No inbound ports.
- **NVMe**: holds the ring buffer (4 minutes per camera), event clips and snapshots
  waiting for upload while the internet is down (within `CCTV_EDGE_DISK_QUOTA_BYTES`,
  20 GB by default; raise it with the drive size) and the outbox. Encrypted at
  provisioning (`ENCRYPT_NVME=yes`).
- **Per staff entrance** (attendance): a dedicated **face camera** at face height,
  1080p or better, WDR, with the entrance lit from the front; or a Hikvision face terminal
  (a later add-on, plan §2). The ceiling cameras of the dining room and kitchen can't do
  face recognition reliably.
- Optional: a 4G router as a backup uplink for critical alerts (plan §9).

What drives the size is the number of streams decoded and analysed at the same time: each
camera's sub-stream at 5 fps for detection, kitchen and attendance cameras at 1280×720,
plus the main stream recorded into the ring buffer (copied, not decoded). **The current
build decodes with ffmpeg on the CPU**; moving decoding to the Orin's hardware decoder
(DeepStream/GStreamer, behind the same interface in `edge/cctv_edge/streams.py`) is
planned and is what makes the camera counts above realistic. Until then, measure the
pilot box (CPU, temperature on the Edge devices page) before promising more than about
8 cameras on any kit. If a box runs hot or drops frames, move to the next kit size rather
than lowering the frame rate below 5 fps.

## 2. Minimum camera spec (11.3)

The site check on the box (`python -m cctv_edge --check`) tests each camera against this
and marks anything below as WARN.

| | Minimum | Better |
|---|---|---|
| Resolution (main stream) | 1920×1080 (2 MP) | 2560×1440 (4 MP) for kitchens |
| Frame rate | 15 fps | 20-25 fps |
| Codec | H.264 or H.265 | H.265 (half the bandwidth to the box) |
| Sub-stream | required, 640×360 or more; **720p or more** for kitchen and staff-entrance cameras | |
| Protocol | RTSP; ONVIF Profile S for discovery | |
| Time | NTP, time zone of the branch (Asia/Kuwait) | |
| Night | IR or low-light; but after-hours rules need some light in the room or IR | |
| Lens | Fixed or varifocal. **Fisheye/ceiling cameras** are fine for presence, counting and security, not for gloves, hair caps or faces (plan §9) | |
| Placement | Kitchen: a view of the hands at the prep line, 2.5-3 m high, not straight down. Counter: the drawer from above. Entrance line: across the door, the whole door width in view | |
| Account | A **read-only** user on the camera or NVR for the AI system | |

## 3. Compatibility (11.3)

What the edge box needs is an RTSP stream; NVR playback, the recordings list and the
site's NVR checks use the vendor's API.

| Vendor | Live analysis and live view (RTSP) | NVR playback, recordings, clock and disk checks | Status |
|---|---|---|---|
| **Hikvision** (cameras and NVRs) | `rtsp://<nvr>:554/Streaming/Channels/<ch>01` (main), `…/<ch>02` (sub) | Yes (ISAPI) | Built for; tested with simulated streams and recorded ISAPI replies. Confirm on the pilot NVR's firmware |
| **Dahua** | `rtsp://<ip>:554/cam/realmonitor?channel=<ch>&subtype=0` (main), `subtype=1` (sub) | No (Dahua's CGI API is different; playback through Dahua is a later add-on) | Expected to work for analysis and live view; not tested |
| **Uniview, Axis, Hanwha, other ONVIF cameras** | The camera's RTSP URL (see its manual or ONVIF device manager) | No | Expected to work for analysis and live view; not tested |
| Cloud-only cameras (no RTSP, e.g. some consumer Wi-Fi cameras) | Not possible | No | Not supported: the box must pull the stream on the LAN |

"Not tested" means no camera of that brand has been connected yet. Before quoting a
client with other brands, connect one camera in the office and run the site check.
