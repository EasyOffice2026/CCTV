# API reference

Generated from the API by `python -m cctv_cloud.openapi` (do not edit by hand; a test
fails when it is stale). Every field and schema is in `cloud/openapi.json`; a running
API shows it interactively at `/docs`. Edge endpoints are described with their
contracts in [contracts/README.md](../contracts/README.md).

## Conventions

Multi-tenant API for the AI CCTV system.

* **Users** sign in at `/v1/auth/login` and send `Authorization: Bearer <access token>`.
  Owners and super-admins must use two-factor authentication.
* **Super-admins** pick the tenant to act on with the `X-Tenant-Id` header.
* **Integrations** (POS, HRM, the operational system) send `X-API-Key: <key>` (created
  under `/v1/api-keys`), e.g. for `/v1/overview` (scope `kpis:read`), and can receive
  signed webhooks (`/v1/webhooks`).
* **The operational system** signs users in with a short-lived token at `/v1/auth/sso`
  (docs/integration/ops-system.md).
* **Edge devices** use `/v1/edge/*` with their device token (see contracts/README.md).
* **Live events**: WebSocket `/v1/ws`; send `{"type": "auth", "token": ...}` first.

* Errors: `{"detail": "..."}` with 401 (not signed in), 403 (not allowed), 404
  (not found, also for other tenants' and branches' rows), 409 (conflict), 422
  (invalid input; `detail` lists the fields), 429 (too many attempts).
* Times are ISO-8601 with an offset (UTC from the API); days are branch-local dates.

## Endpoints

`?` marks optional query parameters.

### admin

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/branches` | List Branches |  |  | `BranchOut[]` |
| POST | `/v1/branches` | Create Branch |  | `BranchIn` | `BranchOut` |
| DELETE | `/v1/branches/{branch_id}` | Delete Branch |  |  |  |
| GET | `/v1/branches/{branch_id}` | Get Branch |  |  | `BranchOut` |
| PATCH | `/v1/branches/{branch_id}` | Update Branch |  | `BranchUpdate` | `BranchOut` |
| GET | `/v1/branches/{branch_id}/working-hours` | List Working Hours |  |  | `WorkingHoursOut[]` |
| PUT | `/v1/branches/{branch_id}/working-hours` | Replace Working Hours |  | `WorkingHoursIn[]` | `WorkingHoursOut[]` |
| GET | `/v1/tenant` | Get Tenant |  |  | `TenantOut` |
| PATCH | `/v1/tenant` | Update Tenant |  | `TenantUpdate` | `TenantOut` |
| GET | `/v1/users` | List Users |  |  | `UserOut[]` |
| POST | `/v1/users` | Creates the user without a password and emails them a link to set one. |  | `UserIn` | `UserOut` |
| PATCH | `/v1/users/{user_id}` | Update User |  | `UserUpdate` | `UserOut` |
| POST | `/v1/users/{user_id}/reset-mfa` | For a lost phone: the user sets up 2FA again at their next login. |  |  |  |

### alerts

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/events/{event_id}/alerts` | Who was alerted about an event, how, and how fast (plan 6.7). |  |  | `DeliveryOut[]` |
| GET | `/v1/me/alert-profile` | Get Alert Profile |  |  | `AlertProfileOut` |
| PUT | `/v1/me/alert-profile` | Update Alert Profile |  | `AlertProfileIn` | `AlertProfileOut` |
| GET | `/v1/me/alert-rules` | List Alert Rules |  |  | `AlertRuleOut[]` |
| POST | `/v1/me/alert-rules` | Create Alert Rule |  | `AlertRuleIn` | `AlertRuleOut` |
| DELETE | `/v1/me/alert-rules/{rule_id}` | Delete Alert Rule |  |  |  |
| PUT | `/v1/me/alert-rules/{rule_id}` | Update Alert Rule |  | `AlertRuleIn` | `AlertRuleOut` |
| POST | `/v1/me/alert-test` | Send a sample message on one channel to the caller (to check the setup). |  | `AlertTest` |  |
| DELETE | `/v1/me/push-subscriptions` | Delete Push Subscription | `endpoint` |  |  |
| PUT | `/v1/me/push-subscriptions` | Register this browser for notifications (again: the keys may have changed). |  | `PushSubscriptionIn` |  |

### attendance

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/attendance` | Work days from `date_from` to `date_to` (inclusive, branch-local dates). | `date_from`, `date_to`, `branch_id`?, `staff_id`? |  | `AttendanceOut[]` |
| DELETE | `/v1/attendance/{staff_id}/{work_date}` | Go back to what face recognition detected for the day. Returns null when nothing |  |  |  |
| PUT | `/v1/attendance/{staff_id}/{work_date}` | Set a day by hand. Sightings keep updating the detected times but no longer change |  | `AttendanceCorrection` | `AttendanceOut` |
| GET | `/v1/counts` | People counts (entrance lines) and zone occupancy, per 5-minute bucket. | `from`, `to`, `branch_id`?, `camera_id`?, `zone_id`? |  | `CountOut[]` |

### auth

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| POST | `/v1/auth/login` | Login |  | `LoginRequest` |  |
| POST | `/v1/auth/logout` | Logout |  |  |  |
| GET | `/v1/auth/me` | Me |  |  | `Me` |
| POST | `/v1/auth/mfa/enable` | Mfa Enable |  | `MfaCode` | `TokenPair` |
| POST | `/v1/auth/mfa/setup` | Mfa Setup |  | `TokenRequest` | `MfaSetup` |
| POST | `/v1/auth/mfa/verify` | Mfa Verify |  | `MfaCode` | `TokenPair` |
| POST | `/v1/auth/password/change` | Change Password |  | `ChangePasswordRequest` |  |
| POST | `/v1/auth/password/forgot` | Forgot Password |  | `ForgotRequest` |  |
| POST | `/v1/auth/password/reset` | Reset Password |  | `ResetRequest` |  |
| POST | `/v1/auth/refresh` | Send the refresh token in the body, or rely on the cookie set at login. |  |  | `TokenPair` |
| POST | `/v1/auth/sso` | Swap a sign-in token from the client's operational system for a session of the |  | `SsoRequest` | `TokenPair` |

### counts

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/heatmaps` | Where people (not staff) stood in the camera's view between `from` and `to`, added | `camera_id`, `from`, `to` |  |  |

### devices

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/cameras` | List Cameras | `branch_id`? |  | `CameraOut[]` |
| POST | `/v1/cameras` | Create Camera |  | `CameraIn` | `CameraOut` |
| DELETE | `/v1/cameras/{camera_id}` | Delete Camera |  |  |  |
| GET | `/v1/cameras/{camera_id}` | Get Camera |  |  | `CameraOut` |
| PATCH | `/v1/cameras/{camera_id}` | Update Camera |  | `CameraUpdate` | `CameraOut` |
| GET | `/v1/cameras/{camera_id}/zones` | List Zones |  |  | `ZoneOut[]` |
| POST | `/v1/cameras/{camera_id}/zones` | Create Zone |  | `ZoneIn` | `ZoneOut` |
| GET | `/v1/edge-devices` | List Devices | `branch_id`? |  | `EdgeDeviceOut[]` |
| POST | `/v1/edge-devices` | Register Device |  | `EdgeDeviceIn` | `PairingCode` |
| DELETE | `/v1/edge-devices/{device_id}` | The device token stops working immediately. |  |  |  |
| POST | `/v1/edge-devices/{device_id}/pairing-code` | New code, e.g. to replace a broken box. The old box keeps working until the new |  |  | `PairingCode` |
| GET | `/v1/nvrs` | List Nvrs |  |  | `NvrOut[]` |
| POST | `/v1/nvrs` | Create Nvr |  | `NvrIn` | `NvrOut` |
| DELETE | `/v1/nvrs/{nvr_id}` | Delete Nvr |  |  |  |
| DELETE | `/v1/zones/{zone_id}` | Delete Zone |  |  |  |
| PUT | `/v1/zones/{zone_id}` | Replace Zone |  | `ZoneIn` | `ZoneOut` |

### edge

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| POST | `/v1/edge/attendance` | Staff recognised by the box; each item is accepted or rejected on its own. |  | `SightingBatch` | `BatchResult` |
| GET | `/v1/edge/config` | Get Config |  |  | `EdgeConfig` |
| POST | `/v1/edge/counts` | Finished count buckets; a bucket sent again replaces the stored one. |  | `CountBatch` | `BatchResult` |
| POST | `/v1/edge/events` | Idempotent by event_id; each event is accepted or rejected on its own. New events |  | `EventBatch` | `EventBatchResponse` |
| GET | `/v1/edge/face-templates` | Templates of the branch's active staff with recorded consent, for recognition on |  |  | `FaceTemplateList` |
| POST | `/v1/edge/heartbeat` | Heartbeat |  | `Heartbeat` | `HeartbeatResponse` |
| POST | `/v1/edge/heatmaps` | Finished heatmap buckets; a bucket sent again replaces the stored one. |  | `HeatmapBatch` | `BatchResult` |
| POST | `/v1/edge/media/upload-urls` | Upload Url |  | `UploadUrlRequest` | `UploadUrlResponse` |
| POST | `/v1/edge/pair` | Swap a one-time pairing code (from the admin panel) for the device token. |  | `PairRequest` | `PairResponse` |
| GET | `/v1/edge/remote` | Long poll: the next request for this box (live view, playback, ...), if any. | `wait`? |  | `RemoteRequest` |
| POST | `/v1/edge/remote/{request_id}/reply` | Answer a request; only the box it was sent to can, and only once. |  | `RemoteReply` |  |
| POST | `/v1/edge/token` | A new device token (plan 7.1). The token used for this call keeps working for a |  |  | `TokenResponse` |

### events

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/events` | Newest first. Results only include branches the caller can access. | `limit`?, `cursor`?, `from`?, `to`?, `branch_id`?, `camera_id`?, `zone_id`?, `feature`?, `event_type`?, `severity`?, `status`?, `review_status`?, `staff_id`?, `pos_match`? |  | `EventPage` |
| GET | `/v1/events/export` | The search results as CSV, newest first, up to `event_export_max_rows` rows. Written | `from`?, `to`?, `branch_id`?, `camera_id`?, `zone_id`?, `feature`?, `event_type`?, `severity`?, `status`?, `review_status`?, `staff_id`?, `pos_match`? |  |  |
| GET | `/v1/events/{event_id}` | Get Event |  |  | `EventOut` |
| GET | `/v1/events/{event_id}/media/{kind}` | A short-lived link to the snapshot or clip. Every call is written to the audit log. | `download`? |  | `MediaLink` |
| PATCH | `/v1/events/{event_id}/review` | Review Event |  | `ReviewIn` | `EventOut` |

### features

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/cameras/{camera_id}/features` | What actually runs on this camera, and which level decided it. |  |  | `ResolvedFeatureOut[]` |
| GET | `/v1/feature-settings` | List Feature Settings | `branch_id`?, `camera_id`? |  | `FeatureSettingOut[]` |
| PUT | `/v1/feature-settings` | Upsert Feature Setting |  | `FeatureSettingIn` | `FeatureSettingOut` |
| DELETE | `/v1/feature-settings/{setting_id}` | Back to inheriting from the level above. |  |  |  |
| GET | `/v1/features` | List Features |  |  | `FeatureOut[]` |

### health

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/healthz` | Process is up (used by the container liveness probe). |  |  |  |
| GET | `/readyz` | Process can serve traffic: the database answers. |  |  |  |

### integrations

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/api-keys` | List Api Keys |  |  | `ApiKeyOut[]` |
| POST | `/v1/api-keys` | Create Api Key |  | `ApiKeyIn` | `ApiKeyCreated` |
| DELETE | `/v1/api-keys/{api_key_id}` | Revoke Api Key |  |  |  |
| GET | `/v1/audit-logs` | The tenant's audit log (plan 7.3): sign-ins, views and exports of footage, reports | `before_id`?, `limit`?, `action`?, `user_id`?, `target_type`?, `target_id`?, `since`?, `until`? |  | `AuditLogOut[]` |
| GET | `/v1/audit-logs/export` | The matching entries as CSV, newest first (up to `event_export_max_rows`). The export | `action`?, `user_id`?, `target_type`?, `target_id`?, `since`?, `until`? |  |  |
| GET | `/v1/payroll/attendance` | Each staff member's work days from `from` to `to` (at most 62 days): status, check-in | `from`, `to`, `branch_id`?, `format`? |  | `PayrollOut` |
| GET | `/v1/platform/audit-logs` | Entries without a tenant (super-admin sign-ins). What a super-admin does for a | `before_id`?, `limit`?, `action`?, `user_id`?, `target_type`?, `target_id`?, `since`?, `until`? |  | `AuditLogOut[]` |
| POST | `/v1/pos/sales` | Sales from a branch's till; a receipt sent again replaces the earlier one. Send |  | `SalesIn` | `SalesResult` |
| GET | `/v1/sso-integrations` | Owners can see how their operational system signs users in; our team sets it up. |  |  | `SsoIntegrationOut[]` |
| POST | `/v1/sso-integrations` | Create Sso Integration |  | `SsoIntegrationIn` | `SsoIntegrationSaved` |
| DELETE | `/v1/sso-integrations/{integration_id}` | Delete Sso Integration |  |  |  |
| PATCH | `/v1/sso-integrations/{integration_id}` | Update Sso Integration |  | `SsoIntegrationUpdate` | `SsoIntegrationSaved` |
| GET | `/v1/webhooks` | List Webhooks |  |  | `WebhookOut[]` |
| POST | `/v1/webhooks` | Create Webhook |  | `WebhookIn` | `WebhookSaved` |
| DELETE | `/v1/webhooks/{webhook_id}` | Delete Webhook |  |  |  |
| PATCH | `/v1/webhooks/{webhook_id}` | Update Webhook |  | `WebhookUpdate` | `WebhookSaved` |
| GET | `/v1/webhooks/{webhook_id}/deliveries` | Newest first (kept 30 days). | `limit`? |  | `WebhookDeliveryOut[]` |
| POST | `/v1/webhooks/{webhook_id}/test` | Send a signed `ping` now and report what the receiver answered. |  |  | `WebhookTestResult` |

### live view

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/cameras/{camera_id}/frame` | A current frame from the camera (e.g. to draw zones on). Not stored anywhere. |  |  |  |
| POST | `/v1/cameras/{camera_id}/live` | Exchange a WebRTC offer for the box's answer (camera sub-stream). The browser closes |  | `LiveIn` | `SdpAnswer` |
| POST | `/v1/cameras/{camera_id}/playback` | Play the NVR's recording from `start` to `end` (at most 2 hours) over WebRTC. |  | `PlaybackIn` | `SdpAnswer` |
| GET | `/v1/cameras/{camera_id}/recordings` | What the NVR recorded for this camera (at most one day per call), for the timeline. | `from`, `to` |  | `RecordingSpan[]` |
| GET | `/v1/live/ice-servers` | STUN/TURN servers for the browser's RTCPeerConnection. |  |  | `IceServers` |

### monitoring

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/incidents` | System problems of the tenant's branches (open first, then newest): boxes offline, | `branch_id`?, `open_only`?, `limit`? |  | `IncidentOut[]` |
| GET | `/v1/platform/incidents` | Every client's open incidents, for our on-call team. |  |  | `IncidentOut[]` |

### overview

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/overview` | One entry per branch the caller can see (or just `branch_id`). Today's figures | `branch_id`?, `day`?, `recent`? |  | `BranchOverview[]` |

### pilot

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/branches/{branch_id}/onboarding` | Where the branch stands in onboarding (plan 11.1), in order: working hours, cameras, |  |  | `Step[]` |
| PUT | `/v1/branches/{branch_id}/pilot` | Day 1 of the branch's official pilot (plan 10), set by our team when shadow mode |  | `PilotStartIn` | `BranchOut` |
| PUT | `/v1/branches/{branch_id}/shadow` | Shadow mode (plan 9.4), set by our team: until `until`, the branch's alerts go only |  | `ShadowIn` | `BranchOut` |
| GET | `/v1/branches/{branch_id}/stream-test` | The setup test (plan 9.2): each camera of the branch over `hours` from `since`. | `since`?, `hours`? |  | `StreamTest` |
| GET | `/v1/pilot/kpis` | The pilot's figures per day and in total against the success criteria (plan 10.3, | `branch_id`, `since`?, `until`? |  | `PilotKpis` |
| GET | `/v1/pilot/log` | The pilot log (plan 10.4), newest first: weekly check-ins, the owner's feedback, | `branch_id`?, `limit`? |  | `PilotNoteOut[]` |
| POST | `/v1/pilot/log` | Add Pilot Note |  | `PilotNoteIn` | `PilotNoteOut` |
| DELETE | `/v1/pilot/log/{note_id}` | Only the author (or our team) removes an entry. |  |  |  |
| GET | `/v1/review-summary` | Review results per day, and per event type, camera and zone with precision and | `branch_id`, `since`?, `until`? |  | `ReviewSummary` |

### platform

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/platform/edge-devices` | Every client's boxes (not retired) with their software version and update state. | `tenant_id`? |  | `PlatformDeviceOut[]` |
| GET | `/v1/platform/edge-releases` | Newest first, with how many boxes run each one and how its rollout is going. |  |  | `ReleaseOut[]` |
| POST | `/v1/platform/edge-releases` | Adds a release. Nothing changes on any box until it is rolled out. |  | `ReleaseIn` | `ReleaseOut` |
| POST | `/v1/platform/edge-releases/{version}/rollout` | Sets the boxes' target version (every client, one client or one box). Each box |  | `RolloutIn` | `RolloutOut` |
| POST | `/v1/platform/edge-releases/{version}/withdraw` | Stops offering it: boxes not on it yet stay where they are. Boxes already on it |  |  |  |
| GET | `/v1/platform/tenants` | List Tenants |  |  | `TenantSummary[]` |
| POST | `/v1/platform/tenants` | Creates the tenant and its first owner, who gets an email to set a password. |  | `TenantCreate` | `TenantCreated` |

### reports

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/reports` | Newest first. | `branch_id`, `period`?, `limit`? |  | `ReportSummary[]` |
| POST | `/v1/reports` | Make (or remake) the report of the period containing `date`, from current data. |  | `ReportRequest` | `ReportOut` |
| GET | `/v1/reports/{report_id}` | Get Report |  |  | `ReportOut` |
| GET | `/v1/reports/{report_id}/file` | Download Report | `format`?, `lang`? |  |  |

### staff

| Method | Path | What | Query | Body | Returns |
|---|---|---|---|---|---|
| GET | `/v1/shifts` | List Shifts | `branch_id`? |  | `ShiftOut[]` |
| POST | `/v1/shifts` | Create Shift |  | `ShiftIn` | `ShiftOut` |
| DELETE | `/v1/shifts/{shift_id}` | Delete Shift |  |  |  |
| PATCH | `/v1/shifts/{shift_id}` | Update Shift |  | `ShiftUpdate` | `ShiftOut` |
| GET | `/v1/staff` | List Staff | `branch_id`?, `status`? |  | `StaffOut[]` |
| POST | `/v1/staff` | Create Staff |  | `StaffIn` | `StaffOut` |
| GET | `/v1/staff/{staff_id}` | Get Staff |  |  | `StaffOut` |
| PATCH | `/v1/staff/{staff_id}` | Update Staff |  | `StaffUpdate` | `StaffOut` |
| DELETE | `/v1/staff/{staff_id}/consent` | Consent withdrawn: the person's face data is deleted (database trigger). |  |  | `StaffOut` |
| PUT | `/v1/staff/{staff_id}/consent` | Record the signed consent form; face enrolment is refused until this is set. |  | `ConsentIn` | `StaffOut` |
| DELETE | `/v1/staff/{staff_id}/face` | Delete Face |  |  | `StaffOut` |
| POST | `/v1/staff/{staff_id}/face` | Enrol (or re-enrol) a face from 1-5 photos. The photos are not stored. |  |  | `StaffOut` |
