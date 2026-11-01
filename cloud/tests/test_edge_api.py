"""Edge device API: pairing, config, heartbeat, uploads and event ingest (plan 3.3-3.5)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from cctv_cloud.services.storage import media_key
from cctv_contracts.enums import MediaKind
from tests.conftest import login

SHA = "ab" * 32


@pytest.fixture
def owner(client, org):
    return login(client, org, "owner_a")


def pair_device(client, org, owner, branch="a1", serial="SN-1") -> dict:
    r = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids[branch]), "name": f"Box {serial}", "serial": serial},
        headers=owner,
    )
    assert r.status_code == 201, r.text
    code = r.json()["pairing_code"]
    paired = client.post("/v1/edge/pair", json={"pairing_code": code.lower(), "serial": serial})
    assert paired.status_code == 200, paired.text
    body = paired.json()
    return {
        "code": code,
        "device_id": body["device_id"],
        "headers": {"Authorization": f"Bearer {body['device_token']}"},
    }


def event(org, device_branch="a1", **overrides) -> dict:
    event_id = overrides.pop("event_id", str(uuid.uuid4()))
    tenant = org.ids["a"] if device_branch.startswith("a") else org.ids["b"]
    snapshot = media_key(
        tenant, org.ids[device_branch], uuid.UUID(event_id), MediaKind.SNAPSHOT, SHA
    )
    return {
        "event_id": event_id,
        "camera_id": str(org.ids[f"cam_{device_branch}"]),
        "feature": "security",
        "event_type": "after_hours_person",
        "severity": "critical",
        "status": "open",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "confidence": 0.8,
        "snapshot_key": snapshot,
        **overrides,
    }


def closed(org, open_event: dict, device_branch="a1") -> dict:
    tenant = org.ids["a"] if device_branch.startswith("a") else org.ids["b"]
    clip = media_key(
        tenant, org.ids[device_branch], uuid.UUID(open_event["event_id"]), MediaKind.CLIP, SHA
    )
    ended = datetime.fromisoformat(open_event["started_at"]) + timedelta(seconds=12)
    return {
        **open_event,
        "status": "closed",
        "ended_at": ended.isoformat(),
        "clip_key": clip,
        "confidence": 0.9,
        "attributes": {"duration_s": 12},
    }


def test_pairing_code_works_once(client, org, owner):
    device = pair_device(client, org, owner)
    again = client.post("/v1/edge/pair", json={"pairing_code": device["code"]})
    assert again.status_code == 401
    assert client.post("/v1/edge/pair", json={"pairing_code": "AAAA-BBBB"}).status_code == 401
    devices = client.get("/v1/edge-devices", headers=owner).json()
    assert devices[0]["paired"] is True


def test_pairing_checks_the_serial(client, org, owner):
    r = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids["a1"]), "name": "Box", "serial": "SN-9"},
        headers=owner,
    )
    code = r.json()["pairing_code"]
    wrong = client.post("/v1/edge/pair", json={"pairing_code": code, "serial": "OTHER"})
    assert wrong.status_code == 409


def test_config_follows_settings(client, org, owner, fakes):
    _, publisher, _ = fakes
    device = pair_device(client, org, owner)
    r = client.get("/v1/edge/config", headers=device["headers"])
    assert r.status_code == 200
    config = r.json()
    assert config["branch_id"] == str(org.ids["a1"])
    assert [c["id"] for c in config["cameras"]] == [str(org.ids["cam_a1"])]
    assert config["cameras"][0]["features"] == []  # everything off by default
    etag = r.headers["etag"]
    assert (
        client.get(
            "/v1/edge/config", headers={**device["headers"], "If-None-Match": etag}
        ).status_code
        == 304
    )

    # Turning security on for the branch: new version, feature listed, edge notified.
    r = client.put(
        "/v1/feature-settings",
        json={
            "scope": "branch",
            "branch_id": str(org.ids["a1"]),
            "feature_key": "security",
            "enabled": True,
            "params": {"after_hours_person": {"min_duration_s": 5}},
        },
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert ("config:" + str(org.ids["a1"])) in [c for c, _ in publisher.messages]
    r = client.get("/v1/edge/config", headers={**device["headers"], "If-None-Match": etag})
    assert r.status_code == 200
    features = r.json()["cameras"][0]["features"]
    assert features == [
        {"key": "security", "params": {"after_hours_person": {"min_duration_s": 5}}}
    ]

    # A camera-level setting wins over the branch.
    r = client.put(
        "/v1/feature-settings",
        json={
            "scope": "camera",
            "camera_id": str(org.ids["cam_a1"]),
            "feature_key": "security",
            "enabled": False,
        },
        headers=owner,
    )
    assert r.status_code == 200
    resolved = client.get(f"/v1/cameras/{org.ids['cam_a1']}/features", headers=owner).json()
    security = next(f for f in resolved if f["key"] == "security")
    assert security == {
        "key": "security",
        "enabled": False,
        "source": "camera",
        "params": {"after_hours_person": {"min_duration_s": 5}},
    }
    assert (
        client.get("/v1/edge/config", headers=device["headers"]).json()["cameras"][0]["features"]
        == []
    )


def test_zones_and_hours_reach_the_edge(client, org, owner):
    device = pair_device(client, org, owner)
    camera = org.ids["cam_a1"]
    zone = client.post(
        f"/v1/cameras/{camera}/zones",
        json={"name": "Back door", "type": "restricted", "points": [[0, 0], [0.5, 0], [0.5, 1]]},
        headers=owner,
    )
    assert zone.status_code == 201, zone.text
    bad = client.post(
        f"/v1/cameras/{camera}/zones",
        json={"name": "Bad", "type": "restricted", "points": [[0, 0], [2, 0], [0.5, 1]]},
        headers=owner,
    )
    assert bad.status_code == 422
    line = client.post(
        f"/v1/cameras/{camera}/zones",
        json={"name": "Door", "type": "entrance_line", "points": [[0, 0.5], [1, 0.5], [1, 1]]},
        headers=owner,
    )
    assert line.status_code == 422  # lines have exactly 2 points
    r = client.put(
        f"/v1/branches/{org.ids['a1']}/working-hours",
        json=[{"weekday": 0, "opens_at": "09:00", "closes_at": "23:00"}],
        headers=owner,
    )
    assert r.status_code == 200
    config = client.get("/v1/edge/config", headers=device["headers"]).json()
    assert config["working_hours"] == [
        {"weekday": 0, "opens_at": "09:00:00", "closes_at": "23:00:00"}
    ]
    assert config["cameras"][0]["zones"][0]["name"] == "Back door"


def test_rtsp_urls_with_passwords_are_refused(client, org, owner):
    r = client.post(
        "/v1/cameras",
        json={
            "branch_id": str(org.ids["a1"]),
            "name": "Leaky",
            "rtsp_main": "rtsp://admin:12345@10.0.0.5/Streaming/Channels/101",
        },
        headers=owner,
    )
    assert r.status_code == 422


def test_heartbeat_updates_device_and_cameras(client, org, owner):
    device = pair_device(client, org, owner)
    now = datetime.now(timezone.utc).isoformat()
    beat = {
        "sent_at": now,
        "software_version": "0.1.0",
        "config_version_applied": 1,
        "uptime_seconds": 60,
        "cameras": [
            {
                "camera_id": str(org.ids["cam_a1"]),
                "health": "online",
                "fps": 5,
                "last_frame_at": now,
            },
            # Another branch's camera: ignored.
            {"camera_id": str(org.ids["cam_a2"]), "health": "online", "fps": 5},
        ],
        "system": {"cpu_percent": 20, "disk_free_bytes": 10**9, "outbox_pending": 0},
    }
    r = client.post("/v1/edge/heartbeat", json=beat, headers=device["headers"])
    assert r.status_code == 200
    assert r.json()["latest_config_version"] >= 1
    cams = {c["id"]: c for c in client.get("/v1/cameras", headers=owner).json()}
    assert cams[str(org.ids["cam_a1"])]["health"] == "online"
    assert cams[str(org.ids["cam_a2"])]["health"] == "offline"
    [listed] = client.get("/v1/edge-devices", headers=owner).json()
    assert listed["status"] == "online" and listed["software_version"] == "0.1.0"


def test_upload_urls_are_scoped_to_the_device(client, org, owner, fakes):
    storage, _, _ = fakes
    device = pair_device(client, org, owner)
    event_id = uuid.uuid4()
    r = client.post(
        "/v1/edge/media/upload-urls",
        json={
            "event_id": str(event_id),
            "kind": "clip",
            "content_type": "video/mp4",
            "size_bytes": 1000,
            "sha256": SHA,
        },
        headers=device["headers"],
    )
    assert r.status_code == 200
    key = r.json()["media_key"]
    assert key.startswith(f"{org.ids['a']}/{org.ids['a1']}/{event_id}/clip-")
    assert storage.puts == [(key, "video/mp4")]


def test_event_ingest_is_idempotent(client, org, owner, fakes):
    _, publisher, _ = fakes
    device = pair_device(client, org, owner)
    opened = event(org)
    for _ in range(2):  # a retry changes nothing
        r = client.post("/v1/edge/events", json={"events": [opened]}, headers=device["headers"])
        assert r.json()["results"][0]["accepted"] is True
    done = closed(org, opened)
    r = client.post("/v1/edge/events", json={"events": [done]}, headers=device["headers"])
    assert r.json()["results"][0]["accepted"] is True
    # A late "open" retry after "closed" is accepted but doesn't reopen the event.
    r = client.post("/v1/edge/events", json={"events": [opened]}, headers=device["headers"])
    assert r.json()["results"][0]["accepted"] is True

    page = client.get("/v1/events", headers=owner).json()
    [stored] = page["items"]
    assert stored["status"] == "closed"
    assert stored["has_clip"] and stored["has_snapshot"]
    assert stored["confidence"] == 0.9
    assert stored["attributes"] == {"duration_s": 12}
    assert sum(c.startswith("events:") for c, _ in publisher.messages) == 3


def test_ingest_rejects_what_the_device_does_not_own(client, org, owner):
    device = pair_device(client, org, owner)
    other_event = str(uuid.uuid4())
    batch = [
        event(org, camera_id=str(org.ids["cam_a2"])),  # same tenant, other branch
        event(org, camera_id=str(org.ids["cam_b1"])),  # other tenant
        event(org, snapshot_key=event(org, event_id=other_event)["snapshot_key"]),  # other event
        event(org, zone_id=str(uuid.uuid4())),  # unknown zone
        event(org),  # fine
    ]
    r = client.post("/v1/edge/events", json={"events": batch}, headers=device["headers"])
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [False, False, False, False, True]
    assert results[0]["error"] == "unknown camera"
    assert results[2]["error"] == "snapshot key was not issued for this event"


def test_event_id_of_another_tenant_cannot_be_overwritten(client, org, owner):
    device_a = pair_device(client, org, owner)
    owner_b = login(client, org, "owner_b")
    device_b = pair_device(client, org, owner_b, branch="b1", serial="SN-B")
    shared_id = str(uuid.uuid4())
    r = client.post(
        "/v1/edge/events",
        json={"events": [event(org, event_id=shared_id)]},
        headers=device_a["headers"],
    )
    assert r.json()["results"][0]["accepted"] is True
    r = client.post(
        "/v1/edge/events",
        json={"events": [event(org, device_branch="b1", event_id=shared_id)]},
        headers=device_b["headers"],
    )
    assert r.json()["results"][0] == {
        "event_id": shared_id,
        "accepted": False,
        "error": "conflicting event",
    }
    [stored] = client.get("/v1/events", headers=owner).json()["items"]
    assert stored["camera_id"] == str(org.ids["cam_a1"])


def test_retired_device_is_locked_out(client, org, owner):
    device = pair_device(client, org, owner)
    assert (
        client.delete(f"/v1/edge-devices/{device['device_id']}", headers=owner).status_code == 204
    )
    assert client.get("/v1/edge/config", headers=device["headers"]).status_code == 401
    assert client.get("/v1/edge/config").status_code == 401
    bogus = {"Authorization": "Bearer cctv_dev_nope"}
    assert client.get("/v1/edge/config", headers=bogus).status_code == 401


def test_watched_areas_and_object_feature_reach_the_edge(client, org, owner):
    """Later phase: objects left or removed are watched in `watch_area` zones."""
    device = pair_device(client, org, owner)
    camera = org.ids["cam_a1"]
    zone = client.post(
        f"/v1/cameras/{camera}/zones",
        json={"name": "Safe", "type": "watch_area", "points": [[0.1, 0.5], [0.4, 0.5], [0.4, 0.9]]},
        headers=owner,
    )
    assert zone.status_code == 201, zone.text
    r = client.put(
        "/v1/feature-settings",
        json={
            "scope": "camera",
            "camera_id": str(camera),
            "feature_key": "object_left_removed",
            "enabled": True,
            "params": {"min_static_s": 300},
        },
        headers=owner,
    )
    assert r.status_code == 200, r.text
    [cam] = client.get("/v1/edge/config", headers=device["headers"]).json()["cameras"][:1]
    assert [z["type"] for z in cam["zones"]] == ["watch_area"]
    assert cam["features"] == [{"key": "object_left_removed", "params": {"min_static_s": 300}}]
    features = {f["key"]: f for f in client.get("/v1/features", headers=owner).json()}
    assert features["object_left_removed"]["availability"] == "pilot"
