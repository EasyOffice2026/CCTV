"""Live event feed over the WebSocket (plan 4.2)."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from cctv_cloud.api.realtime import CLOSE_UNAUTHORIZED
from tests.conftest import login
from tests.test_edge_api import event, pair_device


def token(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


@contextmanager
def socket(client, message: dict) -> Iterator[WebSocketTestSession]:
    with client.websocket_connect("/v1/ws") as ws:
        ws.send_json(message)
        yield ws


def auth(headers: dict[str, str], tenant_id=None) -> dict:
    message = {"type": "auth", "token": token(headers)}
    if tenant_id:
        message["tenant_id"] = str(tenant_id)
    return message


def closed_with(ws: WebSocketTestSession) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    return exc.value.code


def test_events_from_the_edge_reach_the_dashboard(client, org):
    owner = login(client, org, "owner_a")
    device = pair_device(client, org, owner)
    with socket(client, auth(owner)) as ws:
        assert ws.receive_json() == {"type": "ready"}
        opened = event(org)
        r = client.post("/v1/edge/events", json={"events": [opened]}, headers=device["headers"])
        assert r.json()["results"][0]["accepted"] is True
        message = ws.receive_json()
        assert message["type"] == "event"
        assert message["event_id"] == opened["event_id"]
        assert message["branch_id"] == str(org.ids["a1"])
        assert message["severity"] == "critical"


def test_users_only_get_events_of_their_branches(client, org, fakes):
    _, publisher, _ = fakes
    reviewer = login(client, org, "reviewer_a")  # branch a1 only
    with socket(client, auth(reviewer)) as ws:
        assert ws.receive_json()["type"] == "ready"
        tenant = org.ids["a"]
        publisher.publish(f"events:{tenant}", {"event_id": "in-a2", "branch_id": org.ids["a2"]})
        publisher.publish(f"events:{org.ids['b']}", {"event_id": "b", "branch_id": org.ids["b1"]})
        publisher.publish(f"events:{tenant}", {"event_id": "in-a1", "branch_id": org.ids["a1"]})
        assert ws.receive_json()["event_id"] == "in-a1"


def test_super_admin_picks_the_tenant(client, org, fakes):
    _, publisher, _ = fakes
    admin = login(client, org, "admin")
    with socket(client, auth(admin)) as ws:
        assert closed_with(ws) == CLOSE_UNAUTHORIZED  # no tenant chosen

    with socket(client, auth(admin, tenant_id=org.ids["b"])) as ws:
        assert ws.receive_json()["type"] == "ready"
        publisher.publish(f"events:{org.ids['b']}", {"event_id": "b", "branch_id": org.ids["b1"]})
        assert ws.receive_json()["event_id"] == "b"


def test_bad_tokens_and_roles_are_refused(client, org):
    with socket(client, {"type": "auth", "token": "not-a-token"}) as ws:
        assert closed_with(ws) == CLOSE_UNAUTHORIZED
    with socket(client, {"type": "hello"}) as ws:
        assert closed_with(ws) == CLOSE_UNAUTHORIZED
    installer = login(client, org, "installer_a")  # can't view events
    with socket(client, auth(installer)) as ws:
        assert closed_with(ws) == CLOSE_UNAUTHORIZED


def test_token_renewal_must_be_the_same_user(client, org):
    owner = login(client, org, "owner_a")
    with socket(client, auth(owner)) as ws:
        assert ws.receive_json()["type"] == "ready"
        renewed = login(client, org, "owner_a", totp_offset=1)  # a code can't be reused
        ws.send_json(auth(renewed))  # renewal: accepted
        ws.send_json(auth(login(client, org, "manager_a")))
        assert closed_with(ws) == CLOSE_UNAUTHORIZED


def test_logged_out_session_cannot_connect(client, org):
    owner = login(client, org, "owner_a")
    assert client.post("/v1/auth/logout", headers=owner).status_code == 204
    with socket(client, auth(owner)) as ws:
        assert closed_with(ws) == CLOSE_UNAUTHORIZED
