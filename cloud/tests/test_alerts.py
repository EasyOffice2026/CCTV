"""Alert rules, routing, sending, retries and delivery reports (plan 6.1-6.5, 6.7)."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.orm import Session

import cctv_cloud.api.edge as edge_api
from cctv_cloud.api.realtime import _visible
from cctv_cloud.config import get_settings
from cctv_cloud.db.models import AlertDelivery, AlertSubscription, PushSubscription
from cctv_cloud.main import app
from cctv_cloud.rbac import Principal
from cctv_cloud.services.alerts import Senders, dispatch, dispatch_all, in_quiet_hours
from cctv_cloud.services.webpush import PushGoneError, get_push_sender
from cctv_cloud.services.whatsapp import WhatsAppError, get_whatsapp
from tests.conftest import login
from tests.test_edge_api import closed, event, pair_device

FCM = "https://fcm.googleapis.com/fcm/send/"
KEYS = {"p256dh": "B" + "x" * 86, "auth": "a" * 22}


class FakePush:
    public_key = "BPUBLICKEY"

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.gone: set[str] = set()

    def send(self, endpoint, p256dh, auth, message) -> None:
        if endpoint in self.gone:
            raise PushGoneError("410")
        self.sent.append((endpoint, message))


class FakeWhatsApp:
    configured = True

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail: WhatsAppError | None = None

    def send_template(self, to, template, language, params, button_suffix) -> str:
        if self.fail is not None:
            raise self.fail
        self.sent.append(
            {
                "to": to,
                "template": template,
                "lang": language,
                "params": params,
                "suffix": button_suffix,
            }
        )
        return f"wamid.{len(self.sent)}"


@pytest.fixture
def channels(client):
    push, whatsapp = FakePush(), FakeWhatsApp()
    app.dependency_overrides[get_push_sender] = lambda: push
    app.dependency_overrides[get_whatsapp] = lambda: whatsapp
    return push, whatsapp


@pytest.fixture
def users(client, org):
    return {name: login(client, org, name) for name in ("owner_a", "manager_a", "reviewer_a")}


@pytest.fixture
def device(client, org, users):
    return pair_device(client, org, users["owner_a"])


def add_rule(client, headers, **body) -> dict:
    r = client.post("/v1/me/alert-rules", json={"channels": ["web"], **body}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def send_events(client, device, *events) -> None:
    r = client.post("/v1/edge/events", json={"events": list(events)}, headers=device["headers"])
    assert r.status_code == 200, r.text
    assert all(x["accepted"] for x in r.json()["results"]), r.text


def deliveries(owner_engine, event_id=None) -> list[AlertDelivery]:
    with Session(owner_engine) as s:
        query = select(AlertDelivery).order_by(AlertDelivery.id)
        if event_id:
            query = query.where(AlertDelivery.event_id == uuid.UUID(event_id))
        return list(s.scalars(query))


def by_user(rows, org) -> set[tuple[str, str, str]]:
    names = {v: k for k, v in org.ids.items()}
    return {(names[d.user_id], d.channel, d.kind) for d in rows}


def test_quiet_hours():
    assert in_quiet_hours(time(23), time(7), time(2))
    assert in_quiet_hours(time(23), time(7), time(23))
    assert not in_quiet_hours(time(23), time(7), time(7))
    assert in_quiet_hours(time(13), time(15), time(14))
    assert not in_quiet_hours(time(13), time(15), time(16))
    assert not in_quiet_hours(None, None, time(2))


def test_rules_are_validated_and_private(client, org, users):
    owner, manager = users["owner_a"], users["manager_a"]
    rule = add_rule(
        client,
        owner,
        feature_key="security",
        min_severity="critical",
        channels=["whatsapp", "web", "web"],
    )
    assert rule["channels"] == ["web", "whatsapp"]
    bad = [
        {"channels": []},
        {"channels": ["sms"]},
        {"channels": ["web"], "quiet_from": "23:00"},
        {"channels": ["web"], "quiet_from": "23:00", "quiet_until": "23:00"},
        {"channels": ["web"], "feature_key": "nope"},
    ]
    for body in bad:
        assert client.post("/v1/me/alert-rules", json=body, headers=owner).status_code == 422
    # A manager only sees branch a1.
    r = client.post(
        "/v1/me/alert-rules",
        json={"channels": ["web"], "branch_id": str(org.ids["a2"])},
        headers=manager,
    )
    assert r.status_code == 404
    # Rules are the user's own.
    assert client.get("/v1/me/alert-rules", headers=manager).json() == []
    assert (
        client.put(
            f"/v1/me/alert-rules/{rule['id']}", json={"channels": ["web"]}, headers=manager
        ).status_code
        == 404
    )
    assert client.delete(f"/v1/me/alert-rules/{rule['id']}", headers=manager).status_code == 404
    updated = client.put(
        f"/v1/me/alert-rules/{rule['id']}",
        json={
            "channels": ["email"],
            "quiet_from": "23:00",
            "quiet_until": "07:00",
            "enabled": False,
        },
        headers=owner,
    )
    assert updated.status_code == 200 and updated.json()["quiet_until"] == "07:00:00"
    assert client.delete(f"/v1/me/alert-rules/{rule['id']}", headers=owner).status_code == 204
    # Installers don't see events, so they get no alert rules.
    installer = login(client, org, "installer_a")
    assert client.get("/v1/me/alert-rules", headers=installer).status_code == 403


def test_profile(client, org, users, channels):
    owner, reviewer = users["owner_a"], users["reviewer_a"]
    profile = client.get("/v1/me/alert-profile", headers=owner).json()
    assert profile["whatsapp_opt_in"] is False and profile["push_public_key"] == "BPUBLICKEY"
    for body in (
        {"phone": "96550000000"},  # no +
        {"whatsapp_opt_in": True},  # no number
        {"phone": "+96550000000", "report_channels": ["whatsapp"]},  # no consent
        {"locale": "fr"},
    ):
        assert client.put("/v1/me/alert-profile", json=body, headers=owner).status_code == 422
    r = client.put(
        "/v1/me/alert-profile",
        json={
            "phone": "+965 5000 0000",
            "whatsapp_opt_in": True,
            "locale": "ar",
            "report_channels": ["whatsapp", "email"],
        },
        headers=owner,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone"] == "+96550000000" and body["whatsapp_opt_in_at"]
    assert body["report_channels"] == ["email", "whatsapp"] and body["locale"] == "ar"
    # Reviewers don't get reports.
    r = client.put("/v1/me/alert-profile", json={"report_channels": ["email"]}, headers=reviewer)
    assert r.status_code == 403


def test_an_event_alerts_the_right_people_once(
    client, org, users, device, channels, fakes, owner_engine
):
    push, whatsapp = channels
    _, publisher, mailer = fakes
    owner, manager, reviewer = users["owner_a"], users["manager_a"], users["reviewer_a"]
    client.put(
        "/v1/me/alert-profile",
        json={"phone": "+96550000001", "whatsapp_opt_in": True},
        headers=owner,
    )
    add_rule(
        client,
        owner,
        feature_key="security",
        channels=["web", "whatsapp", "email"],
        notify_resolved=True,
    )
    add_rule(client, owner, feature_key="security", channels=["web"])  # overlaps: still once
    add_rule(client, manager, channels=["web", "whatsapp"])  # no number: web only
    add_rule(client, reviewer, feature_key="kitchen_compliance")  # other feature
    r = client.put(
        "/v1/me/push-subscriptions", json={"endpoint": FCM + "owner", "keys": KEYS}, headers=owner
    )
    assert r.status_code == 204

    intrusion = event(org)
    send_events(client, device, intrusion)
    rows = deliveries(owner_engine, intrusion["event_id"])
    assert by_user(rows, org) == {
        ("owner_a", "web", "alert"),
        ("owner_a", "whatsapp", "alert"),
        ("owner_a", "email", "alert"),
        ("manager_a", "web", "alert"),
    }
    assert all(d.status == "sent" and d.latency_ms is not None and d.attempts == 1 for d in rows)
    assert all(d.latency_ms < 10_000 for d in rows)
    assert [m["template"] for m in whatsapp.sent] == ["cctv_alert"]
    assert whatsapp.sent[0]["suffix"] == intrusion["event_id"]
    assert whatsapp.sent[0]["params"][:2] == ["Person after hours", "Branch a1"]
    assert [e for e, _ in push.sent] == [FCM + "owner"]
    assert push.sent[0][1]["url"].endswith(f"/events/?event={intrusion['event_id']}")
    assert any(
        t == "owner_a@example.com" and "Critical: Person after hours" in s
        for t, s, _ in mailer.sent
    )
    toasts = [m for _, m in publisher.messages if m.get("type") == "alert"]
    assert {m["user_id"] for m in toasts} == {str(org.ids["owner_a"]), str(org.ids["manager_a"])}

    # The same event again (a retry, then closed): no repeat; the owner asked for "resolved".
    send_events(client, device, intrusion, closed(org, intrusion))
    rows = deliveries(owner_engine, intrusion["event_id"])
    assert len(rows) == 7
    assert {(c, k) for u, c, k in by_user(rows, org) if k == "resolved"} == {
        ("web", "resolved"),
        ("whatsapp", "resolved"),
        ("email", "resolved"),
    }
    assert whatsapp.sent[-1]["template"] == "cctv_alert_resolved"

    listed = client.get(f"/v1/events/{intrusion['event_id']}/alerts", headers=manager).json()
    assert len(listed) == 7 and {d["user_name"] for d in listed} == {"owner_a", "manager_a"}
    other = login(client, org, "owner_b")
    assert (
        client.get(f"/v1/events/{intrusion['event_id']}/alerts", headers=other).status_code == 404
    )


def test_what_does_not_alert(client, org, users, device, channels, owner_engine):
    owner, manager = users["owner_a"], users["manager_a"]
    add_rule(client, manager, min_severity="warning")
    add_rule(client, owner, quiet_from="00:00", quiet_until="23:59")
    other_branch = pair_device(client, org, owner, branch="a2", serial="SN-2")
    send_events(client, other_branch, event(org, device_branch="a2", severity="warning"))
    info = event(org, severity="info")
    late = event(org, started_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    send_events(client, device, info, late)
    rows = deliveries(owner_engine)
    # a2: not the manager's branch, and the owner's quiet hours hold back warnings.
    # info: below both thresholds. late: arrived two hours after it started.
    assert rows == []
    # Critical alerts get through quiet hours.
    critical = event(org)
    send_events(client, device, critical)
    assert by_user(deliveries(owner_engine), org) == {
        ("owner_a", "web", "alert"),
        ("manager_a", "web", "alert"),
    }
    # Cooldown: the same camera and event type again soon after.
    send_events(client, device, event(org))
    assert len(deliveries(owner_engine)) == 2
    # A deactivated rule or user gets nothing.
    with Session(owner_engine) as s, s.begin():
        s.execute(update(AlertSubscription).values(enabled=False))
    send_events(client, device, event(org, event_type="restricted_area_entry"))
    assert len(deliveries(owner_engine)) == 2


def test_failures_are_retried_then_given_up(
    client, org, users, device, channels, fakes, owner_engine, monkeypatch
):
    push, whatsapp = channels
    owner = users["owner_a"]
    client.put(
        "/v1/me/alert-profile",
        json={"phone": "+96550000001", "whatsapp_opt_in": True},
        headers=owner,
    )
    add_rule(client, owner, channels=["whatsapp", "web"])
    client.put(
        "/v1/me/push-subscriptions", json={"endpoint": FCM + "gone", "keys": KEYS}, headers=owner
    )
    push.gone.add(FCM + "gone")
    whatsapp.fail = WhatsAppError("WhatsApp API 503", retry=True)
    first = event(org)
    send_events(client, device, first)
    rows = {d.channel: d for d in deliveries(owner_engine)}
    assert rows["web"].status == "sent"  # the broken browser doesn't fail the alert
    assert rows["whatsapp"].status == "queued" and rows["whatsapp"].attempts == 1
    assert "503" in rows["whatsapp"].error
    with Session(owner_engine) as s:  # the expired browser subscription is removed
        assert s.scalar(select(PushSubscription.id)) is None

    # Not due yet; then due, and it works.
    senders = Senders(fakes[1], push, whatsapp, fakes[2])
    assert dispatch(org.ids["a"], senders) == 0
    whatsapp.fail = None
    later = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert dispatch(org.ids["a"], senders, now=later) == 1
    retried = {d.channel: d for d in deliveries(owner_engine)}["whatsapp"]
    assert retried.status == "sent" and retried.attempts == 2

    # A permanent error fails at once.
    whatsapp.fail = WhatsAppError("WhatsApp API 400 131026: not a WhatsApp user", retry=False)
    send_events(client, device, event(org, event_type="restricted_area_entry"))
    failed = [d for d in deliveries(owner_engine) if d.status == "failed"]
    assert len(failed) == 1 and "131026" in failed[0].error

    # The per-user hourly limit.
    whatsapp.fail = None
    monkeypatch.setattr(get_settings(), "whatsapp_max_per_hour", 1)
    send_events(client, device, event(org, event_type="after_hours_motion"))
    limited = [d for d in deliveries(owner_engine) if d.error == "rate limit reached"]
    assert len(limited) == 1 and limited[0].status == "failed"


def test_the_worker_sends_what_the_api_could_not(
    client, org, users, device, channels, fakes, owner_engine, monkeypatch
):
    push, whatsapp = channels
    add_rule(client, users["owner_a"], channels=["email"])
    # Queued but not sent, as if the API process stopped right after the commit.
    monkeypatch.setattr(edge_api, "dispatch", lambda *args, **kwargs: None)
    send_events(client, device, event(org))
    assert [d.status for d in deliveries(owner_engine)] == ["queued"]
    assert dispatch_all(Senders(fakes[1], push, whatsapp, fakes[2])) == 1
    assert [d.status for d in deliveries(owner_engine)] == ["sent"]


def test_whatsapp_delivery_reports(client, org, users, device, channels, owner_engine, monkeypatch):
    owner = users["owner_a"]
    client.put(
        "/v1/me/alert-profile",
        json={"phone": "+96550000001", "whatsapp_opt_in": True},
        headers=owner,
    )
    add_rule(client, owner, channels=["whatsapp"])
    send_events(client, device, event(org))
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", SecretStr("app-secret"))
    monkeypatch.setattr(get_settings(), "whatsapp_verify_token", SecretStr("verify-me"))

    params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}
    assert client.get("/v1/webhooks/whatsapp", params=params).text == "42"
    params["hub.verify_token"] = "wrong"
    assert client.get("/v1/webhooks/whatsapp", params=params).status_code == 403

    status = {
        "id": "wamid.1",
        "status": "delivered",
        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
    }
    body = json.dumps({"entry": [{"changes": [{"value": {"statuses": [status]}}]}]}).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    bad = client.post(
        "/v1/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=00"}
    )
    assert bad.status_code == 403
    ok = client.post(
        "/v1/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert ok.status_code == 200
    (row,) = deliveries(owner_engine)
    assert row.status == "delivered" and row.delivered_at is not None


def test_push_subscriptions_and_test_messages(client, org, users, channels, fakes):
    push, _ = channels
    owner = users["owner_a"]
    evil = {"endpoint": "https://169.254.169.254/latest", "keys": KEYS}
    assert client.put("/v1/me/push-subscriptions", json=evil, headers=owner).status_code == 422
    sub = {"endpoint": FCM + "x", "keys": KEYS}
    assert client.put("/v1/me/push-subscriptions", json=sub, headers=owner).status_code == 204
    assert client.put("/v1/me/push-subscriptions", json=sub, headers=owner).status_code == 204

    assert (
        client.post("/v1/me/alert-test", json={"channel": "web"}, headers=owner).status_code == 204
    )
    assert push.sent[-1][1]["title"] == "Test alert"
    # No WhatsApp number yet.
    r = client.post("/v1/me/alert-test", json={"channel": "whatsapp"}, headers=owner)
    assert r.status_code == 502
    assert (
        client.post("/v1/me/alert-test", json={"channel": "email"}, headers=owner).status_code
        == 204
    )
    assert fakes[2].sent[-1][0] == "owner_a@example.com"

    r = client.delete("/v1/me/push-subscriptions", params={"endpoint": FCM + "x"}, headers=owner)
    assert r.status_code == 204
    client.post("/v1/me/alert-test", json={"channel": "web"}, headers=owner)
    assert len(push.sent) == 1


def test_socket_alerts_go_only_to_their_user():
    me, other, branch = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    principal = Principal(
        tenant_id=uuid.uuid4(), permissions=frozenset(), branch_ids=None, user_id=me
    )
    event_message = {"branch_id": str(branch), "event_id": "x"}
    assert _visible(principal, event_message)
    assert _visible(principal, {**event_message, "type": "alert", "user_id": str(me)})
    assert not _visible(principal, {**event_message, "type": "alert", "user_id": str(other)})
    # Test messages belong to no branch.
    assert _visible(principal, {"type": "alert", "user_id": str(me), "branch_id": ""})
