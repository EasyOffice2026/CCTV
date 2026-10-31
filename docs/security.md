# Security (plan Phase 7)

What protects the system, where each control lives in the code, what the deployment must
provide, and the checklist to run before go-live and after changes. Privacy (what personal
data exists, consent, retention, signage) is in [privacy/](privacy/README.md).

## 1. Transport (7.1)

| Path | Protection | Where |
|---|---|---|
| Browser → dashboard, browser → API | TLS, ended by the load balancer or reverse proxy in front of nginx and the API. HSTS (`Strict-Transport-Security`, 1 year) from nginx and from the API in staging/production. The API refuses to start when `CCTV_WEB_URL` or `CCTV_S3_ENDPOINT_URL` isn't `https://` | `web/nginx.conf.template`, `cloud/cctv_cloud/main.py` (`SecurityHeaders`), `cloud/cctv_cloud/config.py` |
| Edge box → cloud | `https://` only (the box refuses a plain-http cloud URL except on the same machine). Per-device bearer token from one-time pairing, stored hashed; **rotated every 30 days** (`CCTV_DEVICE_TOKEN_DAYS`) with a 10-minute grace period for the old token. Retiring or re-pairing a box ends every token at once | `edge/cctv_edge/settings.py`, `edge/cctv_edge/token_store.py`, `cloud/cctv_cloud/api/edge.py` |
| Edge box → object storage | Pre-signed `https` URLs (15 min for uploads); the device token is never sent there | `edge/cctv_edge/cloud.py` |
| Browser → object storage | Pre-signed URLs, 5 minutes, every one written to the audit log | `cloud/cctv_cloud/api/events.py` |
| Live video | WebRTC (DTLS-SRTP) from the box's go2rtc to the browser; signalling through the API | `web/src/lib/webrtc.ts`, `edge/cctv_edge/remote.py` |
| Edge box ↔ cameras, NVR | RTSP/HTTP on the branch LAN (the cameras don't do TLS). Read-only NVR user; passwords on the box only (§4) | — |
| API → database, Redis | Private network. Add `?sslmode=verify-full` to `CCTV_DATABASE_URL` and use `rediss://` when they are not on the same host (`security-check` warns) | `cloud/cctv_cloud/services/security_check.py` |
| Support access to boxes | Tailscale (WireGuard) tailnet; no inbound ports on the branch router | `infra/edge/provision.sh` |

mTLS between box and cloud is not used: device tokens give the same per-device identity and
revocation with less to go wrong at a branch (no certificate expiry on an unattended box).

Every API response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, `Cache-Control: no-store`, `X-Frame-Options: DENY` and a `Content-Security-
Policy` of `default-src 'none'`. The dashboard's nginx sets a CSP that only allows media
from the storage origin, `Referrer-Policy: no-referrer` (pre-signed links never leak) and
`frame-ancestors 'self'` (to be widened to the Mudawara ops system in Phase 8).

## 2. Encryption at rest (7.2)

| Data | How | Where / what to set |
|---|---|---|
| Database | The managed PostgreSQL service's disk encryption (AWS RDS: enable "storage encryption" with a KMS key at creation; it can't be turned on later) | Terraform (plan 1.4) |
| Clips, snapshots | Bucket default encryption, SSE-S3 or SSE-KMS. `security-check --online` fails when the bucket has none. MinIO in the dev stack doesn't encrypt | `cloud/cctv_cloud/services/storage.py` (`default_encryption`) |
| Face templates (biometric) | Application-level: AES-256-GCM per template, bound to the tenant and staff member (a template copied to another row doesn't decrypt). Key from `CCTV_FACE_KEY`: `aws-kms:<key>` (envelope encryption: a KMS data key per template, only its wrapped form stored, decrypts logged in CloudTrail) or `local:<id>` (`python -m cctv_cloud.cli face-key`; keep it in the secrets manager). Production refuses to start without one. Rotation: `rotate-face-keys` | `cloud/cctv_cloud/services/face_keys.py` |
| TOTP secrets | Fernet with a key derived from `CCTV_SECRET_KEY` | `cloud/cctv_cloud/security.py` |
| Passwords, tokens, API keys, pairing codes | Argon2id (passwords) or SHA-256 (high-entropy secrets); never stored in clear | `cloud/cctv_cloud/security.py` |
| Edge NVMe (ring buffer, clips waiting for upload, outbox) | `ENCRYPT_NVME=yes` in `provision.sh`: LUKS2 with a key file on the internal eMMC, unlocked at boot. Protects a drive that is removed, swapped or discarded. A stolen *whole* box can still boot: Jetson disk encryption at flash time (JetPack's `disk_encryption` with a fused key) covers that and is a Phase 9 install decision | `infra/edge/provision.sh` |
| Reports (contain attendance per person) | Stored as JSON in the database (covered by database encryption); files are rendered on download, never stored | `cloud/cctv_cloud/api/reports.py` |

## 3. Audit log (7.3)

Every sign-in (and failed sign-in, including wrong 2FA codes), view and download of a
snapshot, clip, live stream, recording, camera frame, report, event list or audit log, and
every change to settings, users, devices, staff, consent, face data and attendance is written
to `audit_logs` **in the same transaction** as the action. The app role can only insert and
read; old entries are removed by the `audit_purge` database function, which keeps at least
a year whatever `CCTV_AUDIT_RETENTION_DAYS` (730) says.

Owners see their tenant's log on the dashboard (**Audit log** page: filters by action,
person, target and dates; CSV export, which is itself logged). Super-admins see what they
did for a client in that client's log, and their own sign-ins in the platform log
(`/v1/platform/audit-logs`).

## 4. Secrets (7.4)

* **Camera and NVR passwords** exist only on the edge box, in `/etc/cctv-edge/credentials.json`
  (mode 600, owned by the service user, mounted as a Docker secret). The cloud stores a
  `credentials_ref` name; the database refuses RTSP URLs that contain `user:password@`
  (check constraint plus API validation), and the box redacts credentials from every log line
  and never writes them into go2rtc's config.
* **Cloud secrets** (`CCTV_SECRET_KEY`, database and S3 keys, WhatsApp token and app secret,
  VAPID key, face keys) come from the secrets manager as one file per secret in
  `CCTV_SECRETS_DIR` (Docker/Kubernetes secrets; AWS Secrets Manager → ECS/EKS secret
  mounts), or as environment variables for the dev stack. The API refuses the dev secret key
  outside dev/test.
* **Git**: `.gitignore` covers `.env`, keys, `credentials.json`, `device_token`; the pre-commit
  hook and CI run **gitleaks** over every commit (`.gitleaks.toml`). The pre-commit hook also
  blocks private keys and large files (footage, weights).
* **Device tokens**: shown once at pairing, hashed in the database, rotated (§1).
* **Browser**: access token in memory only; refresh token in an HttpOnly, SameSite=Strict
  cookie; pre-signed media links expire in 5 minutes.

## 5. Consent (7.5)

Face enrolment is refused by the API *and* by a database trigger unless the staff member has
a recorded consent date; the consent form text is in [privacy/consent-form.md](privacy/consent-form.md).
Withdrawing consent or marking the person as **left** deletes their face template at once
(database trigger `staff_purge_biometrics`), bumps the branch config version so the box
drops the template from memory within a minute, and is written to the audit log. Faces
that match nobody are discarded on the box without being stored, logged or sent. Enrolment
photos are embedded in memory on the box and never written to disk; only the encrypted
embedding is kept.

## 6. Pen-test checklist (7.6)

Automated, run by CI on every change (`cloud/tests/test_security.py`,
`test_tenant_isolation.py`, `test_permissions.py`, `test_auth.py`, `edge/tests/test_security.py`):

| Check (OWASP 2021 category) | How it is tested |
|---|---|
| A01 Broken access control: every route needs credentials | Every path in the OpenAPI description is called without credentials and must answer 401 (except the sign-in, reset and pairing routes); the WebSocket closes on a bad token |
| A01 IDOR across tenants | Every route with an id in its path is called by tenant A's owner with tenant B's ids (branch, camera, zone, NVR, device, event, staff, shift, report, alert rule, setting, API key, user), with valid bodies, and must answer 403/404 and change nothing |
| A01 IDOR across branches | The same sweep by a branch manager against another branch's rows |
| A01 Row-level security | Every table with `tenant_id` has a policy; the app role can't bypass it, insert for another tenant, or link to another tenant's rows; the audit log is append-only |
| A01 IDOR on clip URLs | Media links are pre-signed per request after the permission check and expire in 5 minutes; keys contain the tenant and branch and are checked against the device's at ingest |
| A02 Cryptographic failures | Face template tamper, wrong-person, wrong-tenant and wrong-key tests; KMS envelope round trip; production refuses plain http and missing keys; bucket encryption check |
| A03 Injection | SQLAlchemy parameters everywhere; CSV cells that would run as spreadsheet formulas are escaped; RTSP URLs with credentials rejected |
| A04 Insecure design | One event per `event_id`; alerts link to the event behind sign-in; WhatsApp webhooks verified by signature |
| A05 Security misconfiguration | `security-check` (settings, TLS, keys, bucket); security headers on every response; Swagger only without CSP |
| A07 Identification and authentication | Lockout after 5 wrong passwords **or 2FA codes**; TOTP replay rejected; refresh-token reuse ends the session; deactivated users locked out at once; password reset links single-use; device tokens rotated, grace period, re-pairing and retirement end old tokens |
| A08 Software and data integrity | Model weights verified by sha256 from their manifest before loading; pinned dependencies and base images; gitleaks in CI |
| A09 Logging and monitoring | Audit log in the same transaction, append-only, retained ≥ 1 year, super-admin sign-ins in the platform log |
| A10 SSRF | Web Push endpoints restricted to the browser vendors' push services; the edge talks only to its configured cloud URL and the pre-signed storage URL |

Manual, before the pilot goes live and after each major change (a half day, by someone who
didn't write the code; record the result in the pilot log):

1. `python -m cctv_cloud.cli security-check --online` on the deployed settings: no errors.
2. With an owner account of a *test* tenant, try the IDOR sweep by hand with Burp/ZAP against
   staging, including the WebSocket and media links copied from another session.
3. Scan the dashboard with ZAP's baseline scan; confirm the CSP, HSTS and cookie flags.
4. `nmap` the branch's public IP and the edge box from the LAN: only 8555 (WebRTC) and
   Tailscale should answer on the box; nothing inbound at the router.
5. Confirm camera passwords appear nowhere in the cloud (database dump search for `@` in
   `rtsp_main`/`rtsp_sub`, logs search for `rtsp://.*:.*@`).
6. Remove a staff member's consent on the dashboard, confirm the box stops recognising them
   within a minute and that `face_templates` has no row for them.
7. Sign in as a super-admin, act in a client tenant, confirm the actions show in that
   client's audit log with the super-admin's name.
8. Dependency audit: `pip-audit` (cloud, edge) and `npm audit` (web); fix criticals.

## 7. Operating it

```bash
python -m cctv_cloud.cli security-check --online   # before go-live and after changes
python -m cctv_cloud.cli face-key                   # new local face key (when not on KMS)
python -m cctv_cloud.cli rotate-face-keys           # after changing CCTV_FACE_KEY
```

Rotation schedule: device tokens automatic (30 days); `CCTV_SECRET_KEY` yearly or at once
if leaked (signs everyone out and needs 2FA re-enrolment; the TOTP secrets are encrypted
with it, so re-encrypt first - a `rotate-secret-key` command is to do); face key yearly
(KMS rotates the master key itself; local keys with `rotate-face-keys`); S3 and database
credentials through the secrets manager with the API restarted; the WhatsApp system-user
token when Meta expires it.

Still open: Terraform for the cloud (plan 1.4) is where RDS encryption, the KMS key, the
bucket policy and the secrets mounts get defined; Jetson flash-time disk encryption (Phase 9);
a `rotate-secret-key` command.
