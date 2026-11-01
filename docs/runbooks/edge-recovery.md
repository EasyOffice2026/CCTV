# Edge recovery (plan 11.5)

For whoever is on call. Each section matches an incident kind on the **System health**
page (super-admins) and the Edge devices page, and the link in on-call messages. Every
fix is noted in the branch's pilot log (Monitoring › Pilot) as an **Issue**.

Remote access to a box: Tailscale (`ssh <box>` over the tailnet; set up by
`provision.sh`). The service: `/opt/cctv-edge/docker-compose.yml`, unit `cctv-edge`.

```bash
sudo systemctl status cctv-edge
sudo docker compose -f /opt/cctv-edge/docker-compose.yml logs --tail 200 edge
sudo docker compose -f /opt/cctv-edge/docker-compose.yml exec edge python -m cctv_edge --check
```

The site check (last command) is the quickest way to see what is wrong; see
[install.md](install.md) for what each line means.

## Device offline

The box stopped sending heartbeats (no contact for 2 minutes; on-call is told after 5).
Nothing is lost while it is only the internet: the box keeps recording events in its
outbox and uploads them when it is back.

1. Is the branch's internet down? Ask the branch, or check other devices there. If so,
   wait; the incident resolves itself when the box is back.
2. Reachable on the tailnet? `tailscale ping <box>`. If yes, the box runs but can't reach
   the cloud: check DNS and the clock (`timedatectl`), then the logs for `cloud` errors
   (401: token refused, see "Re-pair a box" below).
3. Not reachable at all: power or network at the branch. Ask someone on site to check the
   box's lights and the cable, and that the UPS is on mains. The hardware watchdog
   reboots a hung box by itself.
4. Still down after a power cycle: replace the box ("Replace a box" below).

## Stream down

A camera has given no picture for 5 minutes while its box is online.

1. Live view of the camera on the dashboard: black or an error?
2. On the box: the site check's line for that camera (`FAIL camera <name>: main stream:
   Connection refused`, `401 Unauthorized`, `timeout`).
   - **Refused / timeout**: the camera or NVR channel is off, unplugged, or its IP changed
     (check the NVR's channel list). PoE budget of the switch exceeded is common after
     adding cameras.
   - **401**: the NVR password changed; update `/etc/cctv-edge/credentials.json` and
     `sudo systemctl restart cctv-edge`.
   - **No video stream / wrong codec**: someone changed the camera's encoding; set it back
     to H.264/H.265 with a sub-stream ([hardware.md](../hardware.md)).
3. The NVR itself: its recordings for that channel (Playback). If the NVR also has nothing,
   the camera is the problem; if the NVR records but the box can't read, the NVR's
   maximum RTSP connections may be reached (close other viewers, e.g. Hik-Connect).

## Disk low

Less than 20 GB free on the box's data disk.

1. `df -h /var/lib/cctv-edge` and `sudo du -sh /var/lib/cctv-edge/*`.
2. Usually it is media waiting to upload because the internet was down (see "Sync
   backlog"): it shrinks once uploads catch up. Media already uploaded is deleted after
   24 h. The outbox keeps within `CCTV_EDGE_DISK_QUOTA_BYTES` (20 GB): over it, uploaded
   media goes first and, only after a very long outage, the oldest events not yet sent
   (logged as errors). Raise the quota on a bigger drive.
3. Something else filling the disk (Docker images, logs): `sudo docker system prune` of
   old images, check `journalctl --disk-usage`.
4. Never delete files in `outbox.sqlite3` or the media folder by hand: events would be
   lost.

## Temperature high

The box reports 85 °C or more. Jetsons slow down when hot, which drops frames.

1. Ask the branch where the box is: closed cupboard, next to the NVR or a heater, in sun?
   Give it air; boxes must not be stacked on the NVR.
2. Dust in the fan or heatsink: clean at the next visit.
3. Still hot in a good place: too many cameras for the kit ([hardware.md](../hardware.md));
   move cameras to a second box.

## Sync backlog

More than 1,000 events, media files or counts waiting to upload.

1. Internet slow or down at the branch? The backlog drains when it recovers; uploads
   retry by themselves.
2. Internet fine: the logs show why uploads fail (`media upload failed`, HTTP 403 from
   storage: the pre-signed URL expired because the clock is wrong: fix NTP).
3. Very large backlog after a long outage: let it drain overnight. Old events arriving
   more than 30 minutes late don't alert anyone (they are in the feed and the reports).

## Re-pair a box

When the box's token is refused (lost, or the box was retired by mistake):

1. Edge devices › the box › **New pairing code** (valid 24 h). The box keeps working with
   its current token until it pairs again.
2. On the box, from the repository's `infra/edge/`:
   `sudo CCTV_EDGE_PAIRING_CODE=XXXX-XXXX ./provision.sh` (safe to run again).
3. Pairing again ends the old tokens at once.

## Replace a box

1. Provision the new box in the office with the branch's pairing code
   ([install.md](install.md) §1), the same `credentials.json` (copy it over the tailnet,
   mode 600) and the models.
2. On site: swap it, same port and power. It pulls the branch's config by itself.
3. Retire the old box on the Edge devices page. Wipe or destroy its NVMe (its key is on
   the old box's eMMC; without the box the drive can't be read).
4. Run the site check, then watch the setup test for a day.

## Update the edge software

```bash
sudo docker compose -f /opt/cctv-edge/docker-compose.yml pull
sudo systemctl restart cctv-edge
```

Pin `CCTV_EDGE_IMAGE` to a release tag in `/etc/cctv-edge/edge.env` rather than
`latest`, update one branch first, and check its Edge devices row (software version,
config current) and its incidents before the others.
