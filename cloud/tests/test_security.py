"""Plan 7: security headers and settings, device token rotation, face template keys, the
audit log, and the pen-test sweeps (docs/security.md): every route needs credentials, and
no route reaches another tenant's or another branch's rows."""

import base64
import csv
import io
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cctv_cloud.config import Settings, get_settings
from cctv_cloud.db.models import AuditLog, EdgeDevice, FaceTemplate
from cctv_cloud.main import app
from cctv_cloud.security import InvalidTokenError
from cctv_cloud.services import face_keys, security_check
from cctv_cloud.services.face_keys import open_embedding, rotate_templates, seal_embedding
from cctv_cloud.worker import purge_audit_logs
from tests.conftest import PASSWORD, login, totp_code
from tests.test_alerts import send_events
from tests.test_edge_api import event, pair_device
from tests.test_staff import add_staff, consent, photos

EMBEDDING = [0.25, -0.5, 0.125, 1.0] * 32  # 128 values, like SFace


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def branch_today() -> date:
    """Today in the test branches' time zone (not this computer's)."""
    return datetime.now(ZoneInfo("Asia/Kuwait")).date()


# --- 7.1: headers and settings ------------------------------------------------------


def test_api_responses_carry_security_headers(client, org, h):
    r = client.get("/v1/auth/me", headers=h("owner_a"))
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in r.headers  # test runs without TLS
    # Swagger UI needs its CDN scripts, so no CSP there.
    assert "content-security-policy" not in client.get("/docs").headers


def test_deployed_settings_refuse_plain_http_and_missing_keys(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("CCTV_"):
            monkeypatch.delenv(name)
    good = {
        "environment": "production",
        "secret_key": "x" * 40,
        "web_url": "https://cctv.example.com",
        "s3_endpoint_url": None,
        "face_key": "aws-kms:alias/cctv-face",
    }
    assert Settings(_env_file=None, **good)
    with pytest.raises(ValidationError, match="CCTV_WEB_URL must be an https"):
        Settings(_env_file=None, **{**good, "web_url": "http://cctv.example.com"})
    with pytest.raises(ValidationError, match="CCTV_S3_ENDPOINT_URL"):
        Settings(_env_file=None, **{**good, "s3_endpoint_url": "http://minio:9000"})
    with pytest.raises(ValidationError, match="CCTV_FACE_KEY"):
        Settings(_env_file=None, **{**good, "face_key": ""})
    with pytest.raises(ValidationError, match="no key 'k2'"):
        Settings(_env_file=None, **{**good, "face_key": "local:k2"})


def test_secrets_can_come_from_files(tmp_path, monkeypatch):
    monkeypatch.delenv("CCTV_SECRET_KEY", raising=False)
    (tmp_path / "cctv_secret_key").write_text("s" * 48)
    monkeypatch.setenv("CCTV_SECRETS_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert get_settings().secret_key.get_secret_value() == "s" * 48
    finally:
        monkeypatch.delenv("CCTV_SECRETS_DIR")
        get_settings.cache_clear()


def test_security_check_lists_problems():
    settings = Settings(
        _env_file=None,
        environment="staging",
        secret_key="x" * 40,
        web_url="https://cctv.example.com",
        s3_endpoint_url="https://s3.example.com",
        database_url="postgresql+psycopg://app@db/cctv?sslmode=verify-full",
        redis_url="redis://redis:6379/0",
    )
    findings = {(f.level, f.message.split()[0]) for f in security_check.check_settings(settings)}
    assert ("warning", "CCTV_REDIS_URL") in findings
    assert ("error", "CCTV_FACE_KEY") in findings  # empty here: derived from the secret key
    assert not any(level == "error" and "https" in msg for level, msg in findings)


class FakeBucket:
    bucket = "cctv-media"

    def __init__(self, algorithm):
        self.algorithm = algorithm

    def default_encryption(self):
        return self.algorithm


def test_security_check_requires_bucket_encryption():
    assert security_check.check_storage(FakeBucket(None))[0].level == "error"
    assert security_check.check_storage(FakeBucket("aws:kms"))[0].level == "ok"


# --- 7.1: device tokens -------------------------------------------------------------


def _heartbeat(client, headers):
    body = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "software_version": "1.0",
        "config_version_applied": 0,
        "uptime_seconds": 1,
        "cameras": [],
        "system": {"cpu_percent": 1, "disk_free_bytes": 1, "outbox_pending": 0},
    }
    return client.post("/v1/edge/heartbeat", json=body, headers=headers)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_device_token_rotation(client, org, h, owner_engine):
    device = pair_device(client, org, h("owner_a"))
    old = device["headers"]
    assert _heartbeat(client, old).json()["rotate_token"] is False

    with Session(owner_engine) as s, s.begin():  # issued 31 days ago
        s.execute(
            update(EdgeDevice).values(
                token_issued_at=datetime.now(timezone.utc) - timedelta(days=31)
            )
        )
    assert _heartbeat(client, old).json()["rotate_token"] is True

    first = client.post("/v1/edge/token", headers=old).json()["device_token"]
    assert _heartbeat(client, _auth(first)).json()["rotate_token"] is False
    assert _heartbeat(client, old).status_code == 200  # grace period

    # The box restarted before saving `first`: it asks again with the old token.
    second = client.post("/v1/edge/token", headers=old).json()["device_token"]
    assert _heartbeat(client, _auth(first)).status_code == 401  # never saved, gone
    assert _heartbeat(client, _auth(second)).status_code == 200
    long_polled = client.get("/v1/edge/remote?wait=1", headers=old)
    assert long_polled.status_code in (200, 204)

    with Session(owner_engine) as s, s.begin():  # grace period over
        s.execute(
            update(EdgeDevice).values(
                previous_token_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )
    assert _heartbeat(client, old).status_code == 401
    assert client.get("/v1/edge/remote?wait=1", headers=old).status_code == 401
    assert _heartbeat(client, _auth(second)).status_code == 200

    # Retiring the box ends every token at once.
    client.delete(f"/v1/edge-devices/{device['device_id']}", headers=h("owner_a"))
    assert _heartbeat(client, _auth(second)).status_code == 401


def test_pairing_again_ends_the_old_tokens(client, org, h):
    owner = h("owner_a")
    device = pair_device(client, org, owner)
    rotated = client.post("/v1/edge/token", headers=device["headers"]).json()["device_token"]
    code = client.post(
        f"/v1/edge-devices/{device['device_id']}/pairing-code", headers=owner
    ).json()["pairing_code"]
    assert _heartbeat(client, device["headers"]).status_code == 200  # old box still works
    assert client.post("/v1/edge/pair", json={"pairing_code": code}).status_code == 200
    assert _heartbeat(client, _auth(rotated)).status_code == 401
    assert _heartbeat(client, device["headers"]).status_code == 401


# --- 7.2: face templates ------------------------------------------------------------


def _local_key(monkeypatch, *ids: str) -> None:
    settings = get_settings()
    keys = {i: SecretStr(base64.b64encode(bytes([n]) * 32).decode()) for n, i in enumerate(ids)}
    monkeypatch.setattr(settings, "face_keys", keys)
    monkeypatch.setattr(settings, "face_key", f"local:{ids[-1]}")


def test_face_templates_are_bound_to_tenant_and_person(monkeypatch):
    _local_key(monkeypatch, "k1")
    tenant, staff, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sealed, key_id = seal_embedding(EMBEDDING, tenant, staff)
    assert key_id == "local:k1"
    assert open_embedding(sealed, key_id, tenant, staff) == pytest.approx(EMBEDDING)
    with pytest.raises(InvalidTokenError):  # copied onto another person's row
        open_embedding(sealed, key_id, tenant, other)
    with pytest.raises(InvalidTokenError):
        open_embedding(sealed, key_id, uuid.uuid4(), staff)
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(InvalidTokenError):
        open_embedding(tampered, key_id, tenant, staff)
    monkeypatch.setattr(get_settings(), "face_keys", {})
    with pytest.raises(InvalidTokenError, match="not configured"):
        open_embedding(sealed, key_id, tenant, staff)


class FakeKms:
    """KMS stand-in: data keys "wrapped" by reversing them, checking the context."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate_data_key(self, KeyId, KeySpec, EncryptionContext):  # noqa: N803 - boto3 names
        self.calls.append("generate")
        assert KeySpec == "AES_256"
        key = bytes(range(32))
        return {"Plaintext": key, "CiphertextBlob": b"wrapped:" + key[::-1]}

    def decrypt(self, CiphertextBlob, EncryptionContext):  # noqa: N803
        self.calls.append("decrypt")
        assert EncryptionContext["purpose"] == "cctv-face-template"
        return {"Plaintext": CiphertextBlob.removeprefix(b"wrapped:")[::-1]}


def test_face_templates_with_kms(monkeypatch):
    kms = FakeKms()
    monkeypatch.setattr(face_keys, "_kms", lambda: kms)
    monkeypatch.setattr(face_keys, "_data_keys", face_keys._DataKeyCache())
    monkeypatch.setattr(get_settings(), "face_key", "aws-kms:alias/cctv-face")
    tenant, staff = uuid.uuid4(), uuid.uuid4()
    sealed, key_id = seal_embedding(EMBEDDING, tenant, staff)
    assert key_id == "aws-kms:alias/cctv-face"
    assert bytes(range(32)) not in sealed  # only the wrapped data key is stored
    for _ in range(2):
        assert open_embedding(sealed, key_id, tenant, staff) == pytest.approx(EMBEDDING)
    assert kms.calls == ["generate", "decrypt"]  # the second open used the cache


def test_face_key_rotation(client, org, h, owner_engine, monkeypatch):
    owner = h("owner_a")
    staff = add_staff(client, owner, org, code="E9")
    assert consent(client, owner, staff["id"]).status_code == 200
    staff_id = uuid.UUID(staff["id"])
    # Enrolled long ago with the original format.
    legacy = face_keys._fernet(b"face-template").encrypt(face_keys._pack(EMBEDDING))
    with Session(owner_engine) as s, s.begin():
        s.add(
            FaceTemplate(
                tenant_id=org.ids["a"],
                staff_id=staff_id,
                embedding_encrypted=legacy,
                key_id=face_keys.LEGACY_KEY_ID,
                model_version="sface-test",
            )
        )
    _local_key(monkeypatch, "2026-10", "2026-11")
    assert rotate_templates([org.ids["a"], org.ids["b"]]) == 1
    assert rotate_templates([org.ids["a"]]) == 0  # nothing left to do
    with Session(owner_engine) as s:
        template = s.scalars(select(FaceTemplate)).one()
    assert template.key_id == "local:2026-11"
    device = pair_device(client, org, owner)
    [fetched] = client.get("/v1/edge/face-templates", headers=device["headers"]).json()["templates"]
    assert fetched["embedding"] == pytest.approx(EMBEDDING)


# --- 7.3: audit log -----------------------------------------------------------------


def test_audit_log_filters_names_and_export(client, org, h, owner_engine):
    owner, manager = h("owner_a"), h("manager_a")
    client.get("/v1/events/export", headers=manager)
    entries = client.get("/v1/audit-logs", headers=owner).json()
    export = next(e for e in entries if e["action"] == "export_events")
    assert export["user_name"] == "manager_a"
    assert export["user_email"] == "manager_a@example.com"

    only = client.get(
        "/v1/audit-logs",
        params={"action": ["login", "export_events"], "user_id": str(org.ids["manager_a"])},
        headers=owner,
    ).json()
    assert {e["action"] for e in only} == {"login", "export_events"}
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert (
        client.get("/v1/audit-logs", params={"since": later.isoformat()}, headers=owner).json()
        == []
    )
    target = client.get(
        "/v1/audit-logs",
        params={"target_type": "user", "target_id": str(org.ids["manager_a"])},
        headers=owner,
    ).json()
    assert target and all(e["target_id"] == str(org.ids["manager_a"]) for e in target)
    assert client.get("/v1/audit-logs?action=nonsense", headers=owner).status_code == 422

    r = client.get("/v1/audit-logs/export", params={"action": "login"}, headers=owner)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="audit-log-')
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows and {row["action"] for row in rows} == {"login"}
    exported = client.get("/v1/audit-logs", params={"action": "export_audit"}, headers=owner)
    assert exported.json()[0]["details"]["filters"] == {"action": ["login"]}

    # Tenant admins only; and never the other tenant's entries.
    assert client.get("/v1/audit-logs", headers=manager).status_code == 403
    theirs = client.get("/v1/audit-logs", headers=h("owner_b")).json()
    assert {e["user_name"] for e in theirs} == {"owner_b"}


def test_super_admin_sign_ins_go_to_the_platform_log(client, org, h):
    admin = h("admin")
    bad = client.post(
        "/v1/auth/login", json={"email": org.emails["admin"], "password": "wrong password"}
    )
    assert bad.status_code == 401
    entries = client.get("/v1/platform/audit-logs", headers=admin).json()
    assert [e["action"] for e in entries][:2] == ["login_failed", "login"]
    assert entries[1]["user_name"] == "admin"
    assert client.get("/v1/platform/audit-logs", headers=h("owner_a")).status_code == 403
    tenant_view = client.get(
        "/v1/audit-logs", headers={**admin, "X-Tenant-Id": str(org.ids["a"])}
    ).json()
    assert all(e["user_id"] != str(org.ids["admin"]) for e in tenant_view)


def test_audit_entries_are_kept_at_least_a_year(client, org, h, owner_engine, monkeypatch):
    h("owner_a")  # one login entry
    old = datetime.now(timezone.utc) - timedelta(days=400)
    with Session(owner_engine) as s, s.begin():
        for days in (800, 400, 200):
            s.add(
                AuditLog(
                    tenant_id=org.ids["a"],
                    action="view_clip",
                    at=datetime.now(timezone.utc) - timedelta(days=days),
                )
            )
    monkeypatch.setattr(get_settings(), "audit_retention_days", 730)
    assert purge_audit_logs() == 1
    monkeypatch.setattr(get_settings(), "audit_retention_days", 1)  # misconfigured
    assert purge_audit_logs() == 1  # the 400-day one; a year is always kept
    with Session(owner_engine) as s:
        ages = s.scalars(select(AuditLog.at)).all()
    assert all(at > old for at in ages) and len(ages) == 2


# --- 3.1 / 7.6: guessing 2FA codes -------------------------------------------------


def test_wrong_2fa_codes_lock_the_account(client, org, owner_engine):
    email = org.emails["owner_a"]

    def challenge() -> str:
        r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
        return r.json()["mfa_token"]

    for _ in range(5):  # a new challenge each time doesn't reset the count
        r = client.post("/v1/auth/mfa/verify", json={"token": challenge(), "code": "000000"})
        assert r.status_code == 401
    good = totp_code(org.totp[email])
    r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 429
    with Session(owner_engine) as s:
        failures = s.scalars(select(AuditLog).where(AuditLog.action == "login_failed")).all()
    assert len(failures) == 5 and failures[0].details == {"stage": "2fa"}
    assert good  # the right code would not have helped while locked


# --- 7.6: pen-test sweeps -----------------------------------------------------------

# Reachable without credentials (they check their own token, code or password).
PUBLIC = {
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("POST", "/v1/auth/login"),
    ("POST", "/v1/auth/logout"),
    ("POST", "/v1/auth/refresh"),
    ("POST", "/v1/auth/mfa/setup"),
    ("POST", "/v1/auth/mfa/enable"),
    ("POST", "/v1/auth/mfa/verify"),
    ("POST", "/v1/auth/password/forgot"),
    ("POST", "/v1/auth/password/reset"),
    ("POST", "/v1/auth/sso"),
    ("POST", "/v1/edge/pair"),
}


def _routes():
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            yield method.upper(), path


def _fill(path: str, values: dict[str, object]) -> str:
    return re.sub(r"{(\w+)}", lambda m: str(values[m.group(1)]), path)


def test_every_route_needs_credentials(client, org):
    values = {
        **{name: uuid.uuid4() for _, path in _routes() for name in re.findall(r"{(\w+)}", path)},
        "work_date": date.today(),
        "kind": "snapshot",
    }
    open_routes = []
    for method, path in _routes():
        if (method, path) in PUBLIC:
            continue
        r = client.request(method, _fill(path, values))
        if r.status_code != 401:
            open_routes.append((method, path, r.status_code))
    assert open_routes == []
    # The WebSocket closes without a valid token too.
    with client.websocket_connect("/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        with pytest.raises(Exception):  # noqa: B017 - closed by the server
            ws.receive_json()


def _victim_rows(client, org, headers, branch: str, serial: str) -> dict[str, object]:
    """One of each kind of row in `branch`, made by `headers` (an owner)."""
    branch_id, camera = org.ids[branch], org.ids[f"cam_{branch}"]

    def made(r, *path: str):
        assert r.status_code in (200, 201), r.text
        body = r.json()
        for part in path:
            body = body[part]
        return body["id"]

    device = pair_device(client, org, headers, branch=branch, serial=serial)
    sent = event(org, device_branch=branch)
    send_events(client, device, sent)
    return {
        "branch_id": branch_id,
        "camera_id": camera,
        "staff_id": add_staff(client, headers, org, branch=branch, code=f"V-{serial}")["id"],
        "shift_id": made(
            client.post(
                "/v1/shifts",
                json={
                    "branch_id": str(branch_id),
                    "name": "Morning",
                    "starts_at": "08:00",
                    "ends_at": "16:00",
                },
                headers=headers,
            )
        ),
        "zone_id": made(
            client.post(
                f"/v1/cameras/{camera}/zones",
                json={"name": "Door", "type": "restricted", "points": [[0, 0], [1, 0], [1, 1]]},
                headers=headers,
            )
        ),
        "nvr_id": made(
            client.post(
                "/v1/nvrs",
                json={"branch_id": str(branch_id), "name": f"NVR {serial}", "host": "10.0.0.2"},
                headers=headers,
            )
        ),
        "device_id": device["device_id"],
        "event_id": sent["event_id"],
        "report_id": made(
            client.post(
                "/v1/reports",
                # The branch's day (Kuwait), not this computer's: they differ around midnight.
                json={"branch_id": str(branch_id), "period": "day", "day": str(branch_today())},
                headers=headers,
            )
        ),
        "rule_id": made(
            client.post("/v1/me/alert-rules", json={"channels": ["web"]}, headers=headers)
        ),
        "setting_id": made(
            client.put(
                "/v1/feature-settings",
                json={
                    "scope": "branch",
                    "branch_id": str(branch_id),
                    "feature_key": "security",
                    "enabled": True,
                },
                headers=headers,
            )
        ),
        "api_key_id": made(
            client.post(
                "/v1/api-keys", json={"name": serial, "scopes": ["events:read"]}, headers=headers
            ),
            "api_key",
        ),
        "webhook_id": made(
            client.post(
                "/v1/webhooks",
                json={
                    "name": serial,
                    "url": "https://ops.example.com/hook",
                    "topics": ["event.new"],
                },
                headers=headers,
            ),
            "webhook",
        ),
        "note_id": made(
            client.post(
                "/v1/pilot/log",
                json={"noted_on": str(date.today()), "kind": "feedback", "body": serial},
                headers=headers,
            )
        ),
        # Set up by our team only; owners are refused before any lookup.
        "integration_id": uuid.uuid4(),
        "work_date": date.today(),
        "kind": "snapshot",
        "request_id": uuid.uuid4(),
    }


SDP = "v=0\r\n"
BODIES = {
    ("PUT", "/v1/attendance/{staff_id}/{work_date}"): {"status": "day_off"},
    ("PATCH", "/v1/branches/{branch_id}"): {"name": "Taken"},
    ("PUT", "/v1/branches/{branch_id}/working-hours"): [
        {"weekday": 0, "opens_at": "08:00", "closes_at": "22:00"}
    ],
    ("PATCH", "/v1/cameras/{camera_id}"): {"name": "Taken"},
    ("POST", "/v1/cameras/{camera_id}/live"): {"sdp_offer": SDP},
    ("POST", "/v1/cameras/{camera_id}/playback"): {
        "sdp_offer": SDP,
        "start": "2026-10-30T10:00:00Z",
        "end": "2026-10-30T10:05:00Z",
    },
    ("POST", "/v1/cameras/{camera_id}/zones"): {
        "name": "Mine",
        "type": "restricted",
        "points": [[0, 0], [1, 0], [1, 1]],
    },
    ("PATCH", "/v1/events/{event_id}/review"): {"review_status": "false_alarm"},
    ("PUT", "/v1/me/alert-rules/{rule_id}"): {"channels": ["web"]},
    ("PATCH", "/v1/sso-integrations/{integration_id}"): {"name": "Taken"},
    ("PUT", "/v1/branches/{branch_id}/shadow"): {"until": None},
    ("PUT", "/v1/branches/{branch_id}/pilot"): {"started_on": None},
    ("PATCH", "/v1/webhooks/{webhook_id}"): {"name": "Taken"},
    ("PATCH", "/v1/shifts/{shift_id}"): {"name": "Taken"},
    ("PATCH", "/v1/staff/{staff_id}"): {"full_name": "Taken"},
    ("PUT", "/v1/staff/{staff_id}/consent"): {"signed_at": "2026-10-01T09:00:00Z"},
    ("PATCH", "/v1/users/{user_id}"): {"full_name": "Taken"},
    ("PUT", "/v1/zones/{zone_id}"): {
        "name": "Taken",
        "type": "restricted",
        "points": [[0, 0], [1, 0], [1, 1]],
    },
}
QUERIES = {
    ("GET", "/v1/cameras/{camera_id}/recordings"): {
        "from": "2026-10-30T00:00:00Z",
        "to": "2026-10-30T01:00:00Z",
    },
}
FILES = {("POST", "/v1/staff/{staff_id}/face"): photos()}


def _sweep(client, attacker: dict[str, str], values: dict[str, object]) -> list[tuple]:
    """Every route with an id in its path, called by `attacker` with the victim's ids.
    Returns the calls that weren't refused with 403 or 404."""
    leaks = []
    for method, path in _routes():
        if "{" not in path or path.startswith(("/v1/edge/", "/v1/platform/")):
            continue
        key = (method, path)
        r = client.request(
            method,
            _fill(path, values),
            json=BODIES.get(key),
            params=QUERIES.get(key),
            files=FILES.get(key),
            headers=attacker,
        )
        if r.status_code not in (403, 404):
            leaks.append((method, path, r.status_code, r.text[:100]))
    return leaks


def test_no_route_reaches_another_tenants_rows(client, org, h):
    victim = _victim_rows(client, org, h("owner_b"), "b1", "SN-B")
    values = {**victim, "user_id": org.ids["owner_b"]}
    assert _sweep(client, h("owner_a"), values) == []
    # Nothing of tenant b changed.
    owner_b = h("owner_b")
    staff = client.get(f"/v1/staff/{victim['staff_id']}", headers=owner_b).json()
    assert staff["full_name"] != "Taken" and staff["consent_signed_at"] is None
    assert (
        client.get(f"/v1/events/{victim['event_id']}", headers=owner_b).json()["review_status"]
        == "new"
    )


def test_no_route_reaches_another_branchs_rows(client, org, h):
    victim = _victim_rows(client, org, h("owner_a"), "a2", "SN-A2")
    values = {**victim, "user_id": org.ids["owner_a"]}
    assert _sweep(client, h("manager_a"), values) == []
