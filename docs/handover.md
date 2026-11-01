# Handover (plan 11.6)

For the person receiving the system, and for us handing it over. Go through it together;
tick each line when the receiver has done it themselves, not when we say it is done.

## 1. Code

- [ ] Repository access transferred (owner or admin on GitHub); branch protection on `main`
      (CI must pass: lint, secrets scan, contracts, edge, ml, cloud with PostgreSQL, web,
      image builds).
- [ ] The receiver clones it, sets up a laptop with [dev-setup.md](dev-setup.md), and runs
      every test suite and the local stack. Note anything that didn't work and fix the doc.
- [ ] Where things are: [architecture.md](architecture.md). The plan with what is done and
      what isn't: [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) (every phase has a status line).
- [ ] Releasing: tag `v*` → `.github/workflows/release.yml` pushes the images and signs
      the edge image ([runbooks/deploy-cloud.md](runbooks/deploy-cloud.md)); edge boxes
      are updated from Administration › Edge software
      ([runbooks/edge-recovery.md](runbooks/edge-recovery.md#update-the-edge-software)).
      Make the first release together, and roll it out to one box.
- [ ] If the repository moves to another GitHub organisation: the images' registry path
      and the boxes' signer change. Set `CCTV_EDGE_UPDATE_SIGNER` and
      `CCTV_EDGE_UPDATE_REPOSITORY` in each box's edge.env (and the defaults in
      `edge/cctv_edge/updater.py` and `infra/edge/docker-compose.yml`) before the first
      release from the new place, or boxes will refuse it.

## 2. Accounts and secrets

Hand over through a password manager or the cloud's secrets manager, never by email or
chat. Rotate what was shared during the project.

| Account | What it is for | Hand over |
|---|---|---|
| Cloud provider (AWS) | Database, storage, KMS face key, containers, secrets | Admin access; MFA on the root account |
| GitHub | Code, CI, container registry | Organisation owner |
| Domain and DNS, TLS certificates | Dashboard address | Registrar access |
| Meta Business / WhatsApp Business Platform | Alert and report messages; templates (cloud/README.md) | Business admin; the system-user token goes in the secrets manager |
| Tailscale | Support access to the boxes | Tailnet admin; review the device list |
| Email provider (once chosen) | Invitations, password resets, on-call | Admin |
| On-call channel | Where incidents are posted | The webhook URL in the secrets manager |
| Model licences | Commercial use of the detection, PPE, drawer and face models (plan §1 #12) | The licence documents, with each model's `model.json` |

- [ ] `CCTV_SECRET_KEY`, the face key (KMS or local), VAPID key, WhatsApp token and app
      secret, metrics token: in the secrets manager, owned by the receiver. Rotating
      `CCTV_SECRET_KEY` signs everyone out and needs TOTP, SSO and webhook secrets
      re-issued ([security.md](security.md) §7).
- [ ] Our team's super-admin accounts removed or handed over; our support accounts in
      client tenants removed (Administration › Users).
- [ ] Edge boxes: the receiver can reach every box over the tailnet and has the
      `credentials.json` of each branch (or the NVR read-only users to recreate them).

## 3. Running it

- [ ] On-call set up (`CCTV_ONCALL_EMAILS` / webhook), and the receiver has handled a test
      incident (unplug a camera for 10 minutes at a branch, with the client's agreement):
      System health page → on-call message → [runbooks/edge-recovery.md](runbooks/edge-recovery.md).
- [ ] Prometheus scraping `/metrics` and its alerts ([runbooks/deploy-cloud.md](runbooks/deploy-cloud.md)).
- [ ] Database backups checked by a restore into a test environment.
- [ ] `python -m cctv_cloud.cli security-check --online` with no errors, and the manual
      pen-test list in [security.md](security.md) §6 done or scheduled.
- [ ] Onboarding a branch ([runbooks/onboarding.md](runbooks/onboarding.md)) and
      retraining a model ([runbooks/retraining.md](runbooks/retraining.md)) walked through.

## 4. Recorded walkthrough

About 90 minutes, screen-recorded, kept with the repository's documents (not in git):

1. The product from the client's side (20 min): overview, live events and review, search
   and clip, live view and playback, alerts on a phone, a report, staff and attendance,
   the Pilot page, Arabic.
2. Administration (15 min): onboarding wizard, cameras and zones, features, users and
   roles, integrations, audit log, shadow mode, system health.
3. The code (30 min): [architecture.md](architecture.md) on screen, then one event from
   the edge rule to the WhatsApp message (`rules.py` → `outbox.py` → `api/edge.py` →
   `services/alerts.py`); row-level security and the tenant sweeps; how a migration and
   an API change are made.
4. Operations (20 min): a release and deploy, an edge software rollout to one box, an edge
   box over the tailnet with the site check, an incident from the System health page, the worker logs, `/metrics`.
5. Open items (5 min): the plan's unticked items and known limits (below).

## 5. Open items at handover

Taken from [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md); update before the session:

- The AWS scripts ([infra/terraform](../infra/terraform/README.md)) have been checked but not yet applied to a real account.
- Hardware video decoding on the Jetson (DeepStream/GStreamer) not built: decoding is on
  the CPU ([hardware.md](hardware.md)).
- Email provider not chosen; WhatsApp waits on Meta's approval (plan 6.3).
- Model training depends on the licence decision ([ai-models.md](ai-models.md) recommends one); no training code in the repository.
- Staff left out of customer counts only when seen in staff zones; no uniform classifier.
- NVR playback and checks are Hikvision only.
- The operational-system integration method still to be confirmed by their technical
  contact (plan 8.1).
