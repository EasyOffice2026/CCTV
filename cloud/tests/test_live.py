"""Live view, playback, recordings and frames through the edge box (plan 4.4-4.6)."""

import base64
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import pytest

from cctv_cloud.config import get_settings
from tests.conftest import login
from tests.test_edge_api import pair_device

JPEG = b"\xff\xd8\xff\xe0 fake jpeg"


def heartbeat(client, device: dict) -> None:
    beat = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "software_version": "1.0.0",
        "config_version_applied": 1,
        "uptime_seconds": 5,
        "cameras": [],
        "system": {"cpu_percent": 5, "disk_free_bytes": 10**9, "outbox_pending": 0},
    }
    r = client.post("/v1/edge/heartbeat", json=beat, headers=device["headers"])
    assert r.status_code == 200, r.text


def default_answer(request: dict) -> dict:
    kind = request["request"]["kind"]
    if kind in ("live", "playback"):
        return {"ok": True, "sdp_answer": f"answer-to:{request['request']['sdp_offer']}"}
    if kind == "recordings":
        start = datetime.fromisoformat(request["request"]["start"])
        return {
            "ok": True,
            "recordings": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(hours=1)).isoformat(),
                }
            ],
        }
    if kind == "frame":
        return {"ok": True, "jpeg_base64": base64.b64encode(JPEG).decode()}
    return {"ok": False, "error": "unsupported request"}


class FakeEdge:
    """Long-polls like the real box (in a thread) and answers with `answer`."""

    def __init__(self, client, device: dict, answer: Callable[[dict], dict]) -> None:
        self.client, self.device, self.answer = client, device, answer
        self.requests: list[dict] = []
        self.reply_status: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            r = self.client.get("/v1/edge/remote?wait=1", headers=self.device["headers"])
            if r.status_code == 204:
                continue
            assert r.status_code == 200, r.text
            request = r.json()
            self.requests.append(request)
            reply = self.client.post(
                f"/v1/edge/remote/{request['id']}/reply",
                json=self.answer(request),
                headers=self.device["headers"],
            )
            self.reply_status.append(reply.status_code)

    def __enter__(self) -> "FakeEdge":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


@pytest.fixture
def owner(client, org):
    return login(client, org, "owner_a")


@pytest.fixture
def box(client, org, owner) -> Iterator[FakeEdge]:
    device = pair_device(client, org, owner)
    heartbeat(client, device)
    with FakeEdge(client, device, default_answer) as edge:
        yield edge


def test_live_view_passes_the_offer_to_the_box_and_is_audited(client, org, owner, box):
    camera = org.ids["cam_a1"]
    r = client.post(f"/v1/cameras/{camera}/live", json={"sdp_offer": "v=0 offer"}, headers=owner)
    assert r.status_code == 200, r.text
    assert r.json() == {"sdp_answer": "answer-to:v=0 offer"}
    [request] = box.requests
    assert request["request"] == {
        "kind": "live",
        "camera_id": str(camera),
        "sdp_offer": "v=0 offer",
    }
    deadline = time.monotonic() + 5
    while not box.reply_status and time.monotonic() < deadline:
        time.sleep(0.01)  # the box records its reply after the viewer already got it
    assert box.reply_status == [204]

    actions = [a["action"] for a in client.get("/v1/audit-logs", headers=owner).json()]
    assert "view_live" in actions


def test_playback_and_recordings(client, org, owner, box):
    camera = org.ids["cam_a1"]
    start = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)
    r = client.post(
        f"/v1/cameras/{camera}/playback",
        json={
            "sdp_offer": "offer",
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
        },
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert box.requests[-1]["request"]["kind"] == "playback"

    r = client.get(
        f"/v1/cameras/{camera}/recordings",
        params={"from": start.isoformat(), "to": (start + timedelta(hours=12)).isoformat()},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    too_long = client.get(
        f"/v1/cameras/{camera}/recordings",
        params={"from": start.isoformat(), "to": (start + timedelta(days=2)).isoformat()},
        headers=owner,
    )
    assert too_long.status_code == 422
    naive = client.get(
        f"/v1/cameras/{camera}/recordings",
        params={"from": "2026-10-30T08:00:00", "to": "2026-10-30T09:00:00"},
        headers=owner,
    )
    assert naive.status_code == 422


def test_frame_for_installers_too(client, org, box):
    installer = login(client, org, "installer_a")
    r = client.get(f"/v1/cameras/{org.ids['cam_a1']}/frame", headers=installer)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert r.content == JPEG


def test_who_may_watch(client, org, owner, box):
    camera_a1, camera_a2 = org.ids["cam_a1"], org.ids["cam_a2"]
    body = {"sdp_offer": "offer"}
    reviewer = login(client, org, "reviewer_a")
    assert (
        client.post(f"/v1/cameras/{camera_a1}/live", json=body, headers=reviewer).status_code == 403
    )
    installer = login(client, org, "installer_a")
    assert (
        client.post(f"/v1/cameras/{camera_a1}/live", json=body, headers=installer).status_code
        == 403
    )
    manager = login(client, org, "manager_a")  # branch a1 only
    assert (
        client.post(f"/v1/cameras/{camera_a2}/live", json=body, headers=manager).status_code == 404
    )
    owner_b = login(client, org, "owner_b")
    assert (
        client.post(f"/v1/cameras/{camera_a1}/live", json=body, headers=owner_b).status_code == 404
    )
    assert box.requests == []


def test_offline_box_and_silent_box(client, org, owner, monkeypatch):
    camera = org.ids["cam_a1"]
    body = {"sdp_offer": "offer"}
    r = client.post(f"/v1/cameras/{camera}/live", json=body, headers=owner)
    assert r.status_code == 503  # no box in the branch

    device = pair_device(client, org, owner)
    heartbeat(client, device)  # online, but nobody polls
    monkeypatch.setattr(get_settings(), "remote_timeout_seconds", 0.3)
    r = client.post(f"/v1/cameras/{camera}/live", json=body, headers=owner)
    assert r.status_code == 504


def test_box_errors_are_passed_on(client, org, owner):
    device = pair_device(client, org, owner)
    heartbeat(client, device)
    refuse = lambda request: {"ok": False, "error": "camera is offline"}  # noqa: E731
    with FakeEdge(client, device, refuse):
        r = client.post(
            f"/v1/cameras/{org.ids['cam_a1']}/live", json={"sdp_offer": "x"}, headers=owner
        )
    assert r.status_code == 502
    assert r.json()["detail"] == "camera is offline"


def test_only_the_addressed_box_can_reply(client, org, owner):
    device = pair_device(client, org, owner)
    heartbeat(client, device)
    other = pair_device(client, org, login(client, org, "owner_b"), branch="b1", serial="SN-B")
    stolen: list[int] = []

    def answer_from_other_box(request: dict) -> dict:
        r = client.post(
            f"/v1/edge/remote/{request['id']}/reply",
            json={"ok": True, "sdp_answer": "evil"},
            headers=other["headers"],
        )
        stolen.append(r.status_code)
        return {"ok": True, "sdp_answer": "real"}

    with FakeEdge(client, device, answer_from_other_box) as edge:
        r = client.post(
            f"/v1/cameras/{org.ids['cam_a1']}/live", json={"sdp_offer": "x"}, headers=owner
        )
    assert r.json() == {"sdp_answer": "real"}
    assert stolen == [404]
    # Replying twice doesn't work either.
    again = client.post(
        f"/v1/edge/remote/{edge.requests[0]['id']}/reply",
        json={"ok": True, "sdp_answer": "late"},
        headers=device["headers"],
    )
    assert again.status_code == 404


def test_ice_servers(client, org, owner):
    r = client.get("/v1/live/ice-servers", headers=owner)
    assert r.status_code == 200
    assert r.json()["ice_servers"] == get_settings().webrtc_ice_servers
    reviewer = login(client, org, "reviewer_a")
    assert client.get("/v1/live/ice-servers", headers=reviewer).status_code == 403
