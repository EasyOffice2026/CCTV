# Install day (plan 9.1-9.3)

For our install team: from the week before the visit to the end of the 48-hour setup test.
Shadow mode, which follows, is in [shadow-mode.md](shadow-mode.md). Tick every box and
keep this list, filled in, in the pilot log.

## 1. Before the visit

- [ ] **Pilot branch agreed** with the owner (plan §1 #1), and the camera inventory from the
      site survey: channel, place, view, model, resolution. Give every camera a real name;
      on site they are all "Camera 01" (plan §0).
- [ ] **NVR problems fixed by the client's installer** (plan §0), or booked for the visit:
  - [ ] Every NVR and camera on NTP, time zone **Asia/Kuwait (UTC+3)**. The survey found
        NVRs 4 minutes and 5 hours off; wrong NVR time breaks playback and attendance.
  - [ ] Egaila NVR "Disk Exception": disk replaced or formatted, recording checked.
  - [ ] A **read-only NVR user** for the AI system (live view and playback only), not admin.
  - [ ] Every camera streaming **H.264 or H.265**, main stream 1080p at 15 fps or better,
        and a sub-stream enabled (the AI reads the sub-stream).
- [ ] **Staff-entrance camera** (or face terminal) installed at face height (plan §9).
- [ ] **Box registered** under Administration › Edge devices for the pilot branch, with its
      serial; note the pairing code (it works once, for 24 h; a new one can be made there).
- [ ] **Box provisioned and paired** in the office: `infra/edge/provision.sh` with
      `FORMAT_NVME=yes ENCRYPT_NVME=yes TAILSCALE_AUTHKEY=… CCTV_EDGE_CLOUD_URL=https://…
      CCTV_EDGE_PAIRING_CODE=… CCTV_EDGE_SERIAL=…` (edge/README.md). The script swaps the
      code for the box's device token. Check the box shows **Online** on the Edge devices
      page and appears on the tailnet.
- [ ] **Cameras and NVR entered** under Administration › Cameras & NVRs, with the RTSP URLs
      **without** user and password, and a `credentials_ref` name (e.g. `nvr`).
- [ ] **Our support account** exists in the client's tenant (invited as Reviewer with the
      pilot branch) and is marked **Support team** under Administration › Users (only a
      super-admin can do that). It gets the alerts during shadow mode.

## 2. On site

- [ ] Box on the **UPS**, **wired** to the branch LAN on the same network as the NVR (no
      Wi-Fi). Note the switch port.
- [ ] Router: nothing inbound is opened. The box only connects out (https to the cloud,
      Tailscale).
- [ ] Camera passwords on the box only:

  ```bash
  sudo nano /etc/cctv-edge/credentials.json   # {"nvr": {"username": "ai-readonly", "password": "…"}}
  sudo chmod 600 /etc/cctv-edge/credentials.json
  ```

- [ ] `sudo systemctl restart cctv-edge` after writing the credentials. The box shows
      **Online** on the Edge devices page within a minute.
- [ ] Host clock: `timedatectl` says `System clock synchronized: yes`.
- [ ] **Site check**, and fix everything marked FAIL before leaving:

  ```bash
  sudo docker compose -f /opt/cctv-edge/docker-compose.yml exec edge python -m cctv_edge --check
  ```

  It checks the pairing and cloud connection, the box's clock against the cloud, disk and
  temperature; every camera's main and sub-stream (codec, resolution, frame rate against
  the minimum spec); and each NVR's clock, time zone, time source and disks. It only reads.
  Inside the container it can't see NTP ("can't tell": check the host as above). Save the
  output (`--json`) in the pilot log.

- [ ] Live view of every camera opens on the dashboard (Monitoring › Live view).
- [ ] Playback of the last hour works for one camera (checks the NVR user and clock).

## 3. Set-up in the dashboard (9.3)

- [ ] **Working hours** of the branch (Settings › Working hours): after-hours security uses them.
- [ ] **Zones** drawn on each camera's live frame (Settings › Zones): kitchen, counter and
      cash drawer, queue, dining, staff area, restricted areas, entrance counting line, and
      **ignore** zones over TVs, mirrors, windows and the street.
- [ ] **Features** switched on per camera (Settings › Features), only the wave-1 ones:
      attendance, kitchen compliance, cashier, after-hours security (plan 10.1).
- [ ] **Staff**: every staff member with employee code and shift; the signed consent form
      ([privacy/consent-form.md](../privacy/consent-form.md)) recorded for each one who
      agreed; **face enrolment only for them** (the dashboard refuses the others).
- [ ] **CCTV notices** put up at the entrances and in the kitchen
      ([privacy/signage.md](../privacy/signage.md)).
- [ ] **Shadow mode** on for the branch (Administration › Branches › Shadow mode, two weeks)
      **before** any alert rule exists, so the client gets nothing while we tune.

## 4. Setup test: 48 hours without dropped streams (9.2)

Administration › Edge devices, with the branch chosen at the top. The window starts at
the box's first heartbeat. A camera passes when, for 48 hours, its stream never restarted,
it was never offline, and the box reported every minute. The bars show each hour (green:
every minute fine; amber: some; red: none).

- [ ] Check it the next morning. A camera with restarts: cable, PoE budget, camera
      firmware, or the NVR's maximum connections. Minutes without heartbeat: the box or
      the internet (the box keeps working and syncs later, but the test can't prove it).
- [ ] After a fix, start a new window from the time of the fix ("From").
- [ ] **Passed** for every camera: record the date, the window and a screenshot in the
      pilot log. This is the brief's test #1.
