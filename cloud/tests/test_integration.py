"""Plan 8: sign-in from the operational system (SSO), the owner overview with its feature
toggles, KPIs for API keys, and webhooks."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, time, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.models import AuditLog, Branch, WebhookDelivery
from cctv_cloud.main import app
from cctv_cloud.services import webhooks
from cctv_cloud.services.alerts import Senders
from cctv_cloud.worker import make_reports
from tests.conftest import login
from tests.test_alerts import FakePush, FakeWhatsApp, send_events
from tests.test_edge_api import closed, event, pair_device

ISSUER = "https://ops.mudawara.example"
KUWAIT = timezone(timedelta(hours=3))


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def as_admin(h, org, tenant="a") -> dict[str, str]:
    return {**h("admin"), "X-Tenant-Id": str(org.ids[tenant])}


def make_integration(client, h, org, **body) -> dict:
    r = client.post(
        "/v1/sso-integrations",
        json={"name": "Mudawara ops", "issuer": ISSUER, "algorithm": "HS256", **body},
        headers=as_admin(h, org),
    )
    assert r.status_code == 201, r.text
    return r.json()


def token(key, email: str, algorithm="HS256", **claims) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": ISSUER,
        "aud": get_settings().sso_expected_audience,
        "sub": "ops-user-17",
        "email": email,
        "iat": now,
        "exp": now + timedelta(seconds=60),
        "jti": uuid.uuid4().hex,
        "amr": ["pwd", "otp"],
        **claims,
    }
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, key, algorithm)


def sso(client, sso_token: str, embedded=False):
    return client.post("/v1/auth/sso", json={"token": sso_token, "embedded": embedded})


# --- 8.1: sign-in ---------------------------------------------------------------------


def test_only_our_team_sets_up_sign_in(client, org, h):
    body = {"name": "x", "issuer": ISSUER, "algorithm": "HS256"}
    assert client.post("/v1/sso-integrations", json=body, headers=h("owner_a")).status_code == 403
    made = make_integration(client, h, org)
    assert made["secret"] and made["integration"]["audience"] == "http://localhost:3000"
    # The owner sees it (never the secret); another tenant doesn't.
    listed = client.get("/v1/sso-integrations", headers=h("owner_a")).json()
    assert [i["issuer"] for i in listed] == [ISSUER] and "secret" not in listed[0]
    assert client.get("/v1/sso-integrations", headers=h("owner_b")).json() == []
    # An issuer belongs to one tenant.
    again = client.post("/v1/sso-integrations", json=body, headers=as_admin(h, org, "b"))
    assert again.status_code == 409
    integration_id = made["integration"]["id"]
    assert (
        client.patch(
            f"/v1/sso-integrations/{integration_id}", json={"enabled": False}, headers=h("owner_a")
        ).status_code
        == 403
    )


def test_sign_in_with_a_token_from_the_ops_system(client, org, h, owner_engine):
    secret = make_integration(client, h, org)["secret"]
    sign_in = token(secret, "OWNER_A@example.com")
    r = sso(client, sign_in)
    assert r.status_code == 200, r.text
    assert "cctv_refresh" in r.headers.get("set-cookie", "")
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json()["email"] == "owner_a@example.com"
    # Used once only.
    assert sso(client, sign_in).status_code == 401
    # Embedded pages get no cookie (another site's frame) and refresh with the body.
    embedded = sso(client, token(secret, "manager_a@example.com", amr=None), embedded=True)
    assert embedded.status_code == 200 and "set-cookie" not in embedded.headers
    refreshed = client.post("/v1/auth/refresh", json={"token": embedded.json()["refresh_token"]})
    assert refreshed.status_code == 200
    with Session(owner_engine) as s:
        entries = s.scalars(
            select(AuditLog).where(AuditLog.tenant_id == org.ids["a"]).order_by(AuditLog.id)
        ).all()
    logins = [e for e in entries if e.action == "login"]
    assert logins[-2].details == {
        "via": "sso",
        "integration": "Mudawara ops",
        "subject": "ops-user-17",
    }
    assert logins[-1].details["embedded"] == "yes"
    failed = [e for e in entries if e.action == "login_failed"]
    assert failed[-1].details["reason"] == "token already used"


def test_bad_sign_in_tokens_are_refused(client, org, h):
    secret = make_integration(client, h, org)["secret"]
    now = datetime.now(timezone.utc)
    bad = {
        "wrong key": token("another-secret-that-is-long-enough!", "owner_a@example.com"),
        "wrong audience": token(secret, "owner_a@example.com", aud="https://elsewhere"),
        "expired": token(
            secret,
            "owner_a@example.com",
            iat=now - timedelta(minutes=10),
            exp=now - timedelta(minutes=5),
        ),
        "lives too long": token(secret, "owner_a@example.com", exp=now + timedelta(hours=1)),
        "no jti": token(secret, "owner_a@example.com", jti=None),
        "unknown issuer": token(secret, "owner_a@example.com", iss="https://other"),
        "unknown user": token(secret, "nobody@example.com"),
        "other tenant's user": token(secret, "owner_b@example.com"),
        "super-admin": token(secret, "admin@example.com"),
        "owner without 2FA": token(secret, "owner_a@example.com", amr=["pwd"]),
        "unsigned": jwt.encode(
            {"iss": ISSUER, "email": "owner_a@example.com"}, None, algorithm="none"
        ),
        "garbage": "not-a-token",
    }
    for reason, sign_in in bad.items():
        assert sso(client, sign_in).status_code == 401, reason
    # Managers don't need 2FA here, as with passwords.
    assert sso(client, token(secret, "manager_a@example.com", amr=None)).status_code == 200
    # A disabled integration signs nobody in.
    integration_id = client.get("/v1/sso-integrations", headers=h("owner_a")).json()[0]["id"]
    client.patch(
        f"/v1/sso-integrations/{integration_id}",
        json={"enabled": False},
        headers=as_admin(h, org),
    )
    assert sso(client, token(secret, "manager_a@example.com")).status_code == 401


def test_deactivated_users_and_new_secrets(client, org, h):
    made = make_integration(client, h, org)
    old = made["secret"]
    client.patch(
        f"/v1/users/{org.ids['manager_a']}", json={"is_active": False}, headers=h("owner_a")
    )
    assert sso(client, token(old, "manager_a@example.com")).status_code == 401
    renewed = client.patch(
        f"/v1/sso-integrations/{made['integration']['id']}",
        json={"new_secret": True},
        headers=as_admin(h, org),
    ).json()["secret"]
    assert renewed != old
    assert sso(client, token(old, "owner_a@example.com")).status_code == 401
    assert sso(client, token(renewed, "owner_a@example.com")).status_code == 200


@pytest.mark.parametrize("algorithm", ["RS256", "ES256"])
def test_sign_in_with_the_ops_systems_public_key(client, org, h, algorithm):
    private = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if algorithm == "RS256"
        else ec.generate_private_key(ec.SECP256R1())
    )
    pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    made = make_integration(client, h, org, algorithm=algorithm, public_key=pem)
    assert made["secret"] is None
    assert sso(client, token(private, "owner_a@example.com", algorithm)).status_code == 200
    # Only the integration's algorithm is accepted, whatever the token's header says.
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": get_settings().sso_expected_audience,
            "email": "owner_a@example.com",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(seconds=60),
            "jti": "forged",
            "amr": ["otp"],
        },
        hashlib.sha256(pem.encode()).digest(),
        algorithm="HS256",
    )
    assert sso(client, forged).status_code == 401


def test_public_keys_are_checked(client, org, h):
    weak = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505 - must be refused
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    for body in (
        {"algorithm": "RS256", "public_key": weak},
        {"algorithm": "ES256", "public_key": "not a key"},
        {"algorithm": "RS256"},
    ):
        r = client.post(
            "/v1/sso-integrations",
            json={"name": "x", "issuer": ISSUER, **body},
            headers=as_admin(h, org),
        )
        assert r.status_code == 422, body


# --- 8.2-8.4: overview and KPIs ----------------------------------------------------------


def enable(client, owner, feature: str, enabled=True, branch=None) -> None:
    body = {"scope": "tenant", "feature_key": feature, "enabled": enabled}
    if branch:
        body = {**body, "scope": "branch", "branch_id": str(branch)}
    r = client.put("/v1/feature-settings", json=body, headers=owner)
    assert r.status_code == 200, r.text


def test_overview_follows_the_feature_toggles(client, org, h):
    owner = h("owner_a")
    for feature in ("security", "kitchen_compliance", "attendance"):
        enable(client, owner, feature)
    enable(client, owner, "kitchen_compliance", False, branch=org.ids["a2"])
    device = pair_device(client, org, owner)
    send_events(
        client,
        device,
        event(org),
        event(org, feature="kitchen_compliance", event_type="missing_gloves", severity="warning"),
    )

    r = client.get("/v1/overview", headers=owner)
    assert r.status_code == 200, r.text
    a1, a2 = r.json()
    assert (a1["branch_name"], a2["branch_name"]) == ("Branch a1", "Branch a2")
    assert a1["features"]["security"] and a1["features"]["kitchen_compliance"]
    assert not a1["features"]["customer_count"] and a1["customers"] is None
    assert a1["security"] == 1 and a1["kitchen_compliance"] == 1
    assert a1["events"] == {"total": 2, "critical": 1, "false_alarms": 0, "unreviewed": 2}
    assert a1["attendance"]["on_time"] == 0 and a1["cashier"] is None
    assert len(a1["recent_events"]) == 2 and a1["reports"] == []
    assert a1["system"]["devices_online"] + a1["system"]["devices_offline"] <= 1
    # Off for branch a2: no section.
    assert not a2["features"]["kitchen_compliance"] and a2["kitchen_compliance"] is None
    assert a2["security"] == 0

    # Turning a feature off hides its events too.
    enable(client, owner, "kitchen_compliance", False)
    a1 = client.get("/v1/overview", params={"branch_id": str(org.ids["a1"])}, headers=owner)
    assert [e["feature"] for e in a1.json()[0]["recent_events"]] == ["security"]


def test_overview_permissions_and_api_keys(client, org, h):
    owner = h("owner_a")
    enable(client, owner, "security")
    manager = client.get("/v1/overview", headers=h("manager_a")).json()
    assert [b["branch_name"] for b in manager] == ["Branch a1"]
    a2 = {"branch_id": str(org.ids["a2"])}
    assert client.get("/v1/overview", params=a2, headers=h("manager_a")).status_code == 404
    assert client.get("/v1/overview", headers=h("reviewer_a")).status_code == 403
    tomorrow = (datetime.now(KUWAIT) + timedelta(days=2)).date().isoformat()
    assert client.get("/v1/overview", params={"day": tomorrow}, headers=owner).status_code == 422

    def key(scope: str) -> dict[str, str]:
        r = client.post("/v1/api-keys", json={"name": scope, "scopes": [scope]}, headers=owner)
        return {"X-API-Key": r.json()["key"]}

    kpis = client.get("/v1/overview", headers=key("kpis:read"))
    assert kpis.status_code == 200
    # Totals only: no event list, no reports, and no other route opens.
    assert kpis.json()[0]["recent_events"] is None and kpis.json()[0]["reports"] is None
    assert client.get("/v1/reports", params=a2, headers=key("kpis:read")).status_code == 403
    assert client.get("/v1/overview", headers=key("events:read")).status_code == 403


# --- 8.4: webhooks ---------------------------------------------------------------------


class FakePoster:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, dict]] = []
        self.fail: webhooks.WebhookError | None = None

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        if self.fail:
            raise self.fail
        self.sent.append((url, json.loads(body), headers | {"_body": body.decode()}))
        return 204


@pytest.fixture
def poster():
    fake = FakePoster()
    app.dependency_overrides[webhooks.get_webhook_poster] = lambda: fake
    yield fake
    app.dependency_overrides.pop(webhooks.get_webhook_poster, None)


def add_webhook(client, owner, **body) -> dict:
    r = client.post(
        "/v1/webhooks",
        json={
            "name": "Ops system",
            "url": "https://ops.mudawara.example/hooks/cctv",
            "topics": ["event.new", "event.closed", "report.ready"],
            **body,
        },
        headers=owner,
    )
    assert r.status_code == 201, r.text
    return r.json()


def verified(secret: str, headers: dict) -> bool:
    expected = hmac.new(
        secret.encode(),
        f"{headers['X-CCTV-Timestamp']}.{headers['_body']}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(headers["X-CCTV-Signature"], f"v1={expected}")


def test_webhook_urls_must_be_public_https(client, org, h, monkeypatch):
    owner = h("owner_a")
    for url in (
        "http://ops.example.com/hook",
        "https://10.0.0.8/hook",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://localhost/hook",
        "https://user:pass@ops.example.com/hook",
        "ftp://ops.example.com",
    ):
        r = client.post(
            "/v1/webhooks", json={"name": "x", "url": url, "topics": ["event.new"]}, headers=owner
        )
        assert r.status_code == 422, url
    assert client.get("/v1/webhooks", headers=h("manager_a")).status_code == 403
    # Names that resolve to private addresses are refused when sending.
    monkeypatch.setattr(
        webhooks.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))]
    )
    with pytest.raises(ValueError, match="public"):
        webhooks.check_url("https://ops.example.com/hook", resolve=True)
    with pytest.raises(webhooks.WebhookError, match="public"):
        webhooks.HttpPoster().post("https://ops.example.com/hook", b"{}", {})


def test_events_are_pushed_signed(client, org, h, poster, owner_engine):
    owner = h("owner_a")
    saved = add_webhook(client, owner, min_severity="warning")
    add_webhook(client, owner, name="Branch a2 only", branch_id=str(org.ids["a2"]))
    device = pair_device(client, org, owner)
    sent = event(org)
    send_events(client, device, sent, event(org, severity="info"))  # info: below the minimum
    send_events(client, device, sent)  # the same event again: no second message
    send_events(client, device, closed(org, sent))

    assert [m["topic"] for _, m, _ in poster.sent] == ["event.new", "event.closed"]
    url, message, headers = poster.sent[0]
    assert url == "https://ops.mudawara.example/hooks/cctv"
    assert verified(saved["secret"], headers) and headers["X-CCTV-Topic"] == "event.new"
    assert message["id"] == headers["X-CCTV-Webhook-Id"] == f"event.new:{sent['event_id']}"
    data = message["data"]
    assert data["event_id"] == sent["event_id"] and data["branch"] == "Branch a1"
    assert data["url"].endswith(f"/events/?event={sent['event_id']}")
    assert not {"snapshot_key", "clip_key", "staff_id"} & data.keys()  # links, not media

    deliveries = client.get(
        f"/v1/webhooks/{saved['webhook']['id']}/deliveries", headers=owner
    ).json()
    assert [d["status"] for d in deliveries] == ["sent", "sent"]
    assert client.post(f"/v1/webhooks/{saved['webhook']['id']}/test", headers=owner).json() == {
        "ok": True,
        "status": 204,
        "error": None,
    }
    assert poster.sent[-1][1]["topic"] == "ping"


def test_failed_webhooks_are_retried(client, org, h, poster, owner_engine):
    owner = h("owner_a")
    hook = add_webhook(client, owner)["webhook"]
    device = pair_device(client, org, owner)
    poster.fail = webhooks.WebhookError("HTTP 503: busy", status=503)
    send_events(client, device, event(org))
    with Session(owner_engine) as s:
        row = s.scalars(select(WebhookDelivery)).one()
    assert (row.status, row.attempts, row.response_status) == ("queued", 1, 503)

    poster.fail = None
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert webhooks.dispatch(org.ids["a"], poster, now=later) == 1
    listed = client.get("/v1/webhooks", headers=owner).json()[0]
    assert listed["last_success_at"] and listed["last_error"] == "HTTP 503: busy"

    # A receiver that refuses (4xx) isn't retried.
    poster.fail = webhooks.WebhookError("HTTP 401: no", retry=False, status=401)
    send_events(client, device, event(org))
    with Session(owner_engine) as s:
        newest = s.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id.desc())).first()
    assert newest.status == "failed"
    test = client.post(f"/v1/webhooks/{hook['id']}/test", headers=owner).json()
    assert test == {"ok": False, "status": 401, "error": "HTTP 401: no"}


def test_reports_are_pushed_when_made(client, org, h, poster, fakes, owner_engine):
    owner = h("owner_a")
    add_webhook(client, owner, topics=["report.ready"], branch_id=str(org.ids["a1"]))
    with Session(owner_engine) as s, s.begin():
        s.execute(update(Branch).values(created_at=datetime.now(timezone.utc) - timedelta(days=1)))
    morning = datetime.combine(datetime.now(KUWAIT).date(), time(3), KUWAIT)
    make_reports(Senders(fakes[1], FakePush(), FakeWhatsApp(), fakes[2]), now=morning)
    assert webhooks.dispatch_all(poster) >= 1
    topics = {(m["topic"], m["data"]["branch"]) for _, m, _ in poster.sent}
    assert topics == {("report.ready", "Branch a1")}
    data = poster.sent[0][1]["data"]
    assert "/reports/?report=" in data["url"] and "staff" not in data
    assert "attendance_pct" in data["totals"]
