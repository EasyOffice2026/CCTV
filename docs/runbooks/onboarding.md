# Onboarding a new client or branch (plan 11.1)

For our sales, install and support team. The dashboard's **Administration › Onboarding**
page shows the same steps for the branch chosen at the top, worked out from what is set
up, with the next step highlighted. This document is the work behind each step.

| # | Step | Who | How | Done when (wizard) |
|---|---|---|---|---|
| 0 | **Survey** | Sales + installer | Visit: camera inventory (channel, place, view, model, resolution), NVR make, model, firmware and disks, network, power, where the box goes, staff entrance; agree the features and the pilot branch. Hardware quote from [../hardware.md](../hardware.md) | (before the wizard) |
| 0 | **Client** | Our team | Administration › Clients › new client: its owner gets an invitation | (before the wizard) |
| 1 | Branch and working hours | Owner or our team | Administration › Branches; Settings › Working hours | The branch has working hours |
| 2 | Cameras and NVR | Our team | Administration › Cameras & NVRs, from the survey: RTSP URLs **without** passwords, a `credentials_ref`, real camera names | At least one camera |
| 3 | Edge box | Our team | Register, provision and pair in the office; install on site ([install.md](install.md)) | A box online |
| 4 | Streaming | Installer | Site check on the box; NVR fixes (NTP, disk, read-only user) | Every camera has a picture |
| 5 | Setup test | Installer | Edge devices › Setup test: 48 h without drops | Passed |
| 6 | Zones | Our team with the owner | Settings › Zones on each camera's live frame | Every camera has a zone |
| 7 | Features | Our team with the owner | Settings › Features: only the agreed ones | Every camera has a feature on |
| 8 | Staff | Owner / managers | Staff with codes and shifts; signed consent forms recorded; faces enrolled | Every consenting staff member enrolled (when attendance is on) |
| 9 | Alert rules | Each user | Settings › Alerts; test messages | At least one rule for the branch |
| 10 | Shadow mode | Our team | 1-2 weeks, alerts only to the support team; daily review ([shadow-mode.md](shadow-mode.md)) | Shadow mode was set |
| 11 | Go live | Our team with the owner | Pilot day 1 ([../pilot/README.md](../pilot/README.md)); train users ([../training/owner-and-managers.md](../training/owner-and-managers.md)) | Pilot day 1 is set |

Also, once per client: the CCTV notices up ([../privacy/signage.md](../privacy/signage.md)),
the consent form agreed with their counsel ([../privacy/consent-form.md](../privacy/consent-form.md)),
WhatsApp templates approved for their number if they want WhatsApp, and, if they have
one, the operational-system integration ([../integration/ops-system.md](../integration/ops-system.md)).

After go-live the client is on normal support: incidents on the System health page and
on-call ([edge-recovery.md](edge-recovery.md)).
