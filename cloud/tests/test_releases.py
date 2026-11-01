"""Plan 2.10: edge software releases, rollouts and the update offer in heartbeats."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AuditLog
from tests.conftest import login
from tests.test_edge_api import pair_device
from tests.test_pilot import beat

IMAGE = "ghcr.io/easyoffice2026/cctv-edge@sha256:" + "a" * 64


@pytest.fixture
def h(client, org):
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def heartbeat(client, org, device, version="0.1.0", update=None, branch="a1") -> dict:
    body = beat(org, camera=f"cam_{branch}")
    body["software_version"] = version
    if update is not None:
        body["update"] = update
    r = client.post("/v1/edge/heartbeat", json=body, headers=device["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def releases(client, h) -> dict[str, dict]:
    r = client.get("/v1/platform/edge-releases", headers=h("admin"))
    assert r.status_code == 200, r.text
    return {x["version"]: x for x in r.json()}


def test_releases_are_for_our_team_only(client, org, h):
    body = {"version": "1.1.0", "image": IMAGE, "notes": "Faster clips"}
    assert (
        client.post("/v1/platform/edge-releases", json=body, headers=h("owner_a")).status_code
        == 403
    )
    r = client.post("/v1/platform/edge-releases", json=body, headers=h("admin"))
    assert r.status_code == 201, r.text
    assert r.json()["running"] == 0
    assert (
        client.post("/v1/platform/edge-releases", json=body, headers=h("admin")).status_code == 409
    )
    for bad in (
        {"version": "1.2.0", "image": "ghcr.io/easyoffice2026/cctv-edge:latest"},  # no digest
        {"version": "latest", "image": IMAGE},
    ):
        r = client.post("/v1/platform/edge-releases", json=bad, headers=h("admin"))
        assert r.status_code == 422, bad
    assert client.get("/v1/platform/edge-releases", headers=h("owner_a")).status_code == 403
    assert client.get("/v1/platform/edge-devices", headers=h("owner_a")).status_code == 403


def test_rollout_offer_report_and_retry(client, org, h, owner_engine):
    box1 = pair_device(client, org, h("owner_a"))
    box2 = pair_device(client, org, h("owner_a"), serial="SN-2")
    box_b = pair_device(client, org, h("owner_b"), branch="b1", serial="SN-B")
    client.post(
        "/v1/platform/edge-releases", json={"version": "1.1.0", "image": IMAGE}, headers=h("admin")
    )
    assert heartbeat(client, org, box1)["update"] is None

    # One box first.
    r = client.post(
        "/v1/platform/edge-releases/1.1.0/rollout",
        json={"tenant_id": str(org.ids["a"]), "device_id": box1["device_id"]},
        headers=h("admin"),
    )
    assert r.json() == {"devices": 1}
    offer = heartbeat(client, org, box1)["update"]
    assert (offer["version"], offer["image"]) == ("1.1.0", IMAGE)
    assert heartbeat(client, org, box2)["update"] is None
    assert releases(client, h)["1.1.0"] | {"created_at": None} == {
        "version": "1.1.0",
        "image": IMAGE,
        "notes": "",
        "created_at": None,
        "withdrawn_at": None,
        "running": 0,
        "updating": 1,
        "failed": 0,
    }

    # The box rolled back: not offered again, shown as failed on the box and the release.
    report = {
        "version": "1.1.0",
        "requested_at": offer["requested_at"],
        "state": "rolled_back",
        "detail": "no heartbeat from the new version in 10 min",
        "at": offer["requested_at"],
    }
    assert heartbeat(client, org, box1, update=report)["update"] is None
    assert heartbeat(client, org, box1, update=report)["update"] is None
    devices = client.get("/v1/edge-devices", headers=h("owner_a")).json()
    [d1] = [d for d in devices if d["id"] == box1["device_id"]]
    assert (d1["target_version"], d1["update_state"]) == ("1.1.0", "rolled_back")
    assert d1["update_detail"] == "no heartbeat from the new version in 10 min"
    assert releases(client, h)["1.1.0"]["failed"] == 1

    # Rolled out again (every box of every client): a new request, so box 1 retries.
    r = client.post("/v1/platform/edge-releases/1.1.0/rollout", json={}, headers=h("admin"))
    assert r.json() == {"devices": 3}
    retry = heartbeat(client, org, box1, update=report)["update"]
    assert retry["version"] == "1.1.0" and retry["requested_at"] != offer["requested_at"]
    assert heartbeat(client, org, box_b, branch="b1")["update"]["version"] == "1.1.0"

    # On the new version: nothing more to offer.
    updated = report | {"requested_at": retry["requested_at"], "state": "updated", "detail": None}
    assert heartbeat(client, org, box1, version="1.1.0", update=updated)["update"] is None
    counts = releases(client, h)["1.1.0"]
    assert (counts["running"], counts["updating"], counts["failed"]) == (1, 2, 0)

    platform = client.get("/v1/platform/edge-devices", headers=h("admin")).json()
    assert {(d["tenant"], d["name"], d["software_version"]) for d in platform} == {
        ("Tenant a", "Box SN-1", "1.1.0"),
        ("Tenant a", "Box SN-2", "0.1.0"),
        ("Tenant b", "Box SN-B", "0.1.0"),
    }

    # Withdrawn: boxes not on it yet are no longer offered it; it can't be rolled out.
    r = client.post("/v1/platform/edge-releases/1.1.0/withdraw", headers=h("admin"))
    assert r.status_code == 204
    assert heartbeat(client, org, box2)["update"] is None
    r = client.post("/v1/platform/edge-releases/1.1.0/rollout", json={}, headers=h("admin"))
    assert r.status_code == 409

    with Session(owner_engine) as s:
        audits = s.scalars(select(AuditLog).where(AuditLog.action == "software_update")).all()
    assert sorted((str(a.tenant_id), a.details["devices"]) for a in audits) == sorted(
        [(str(org.ids["a"]), 1), (str(org.ids["a"]), 2), (str(org.ids["b"]), 1)]
    )


def test_rollout_checks_its_target(client, org, h):
    client.post(
        "/v1/platform/edge-releases", json={"version": "1.1.0", "image": IMAGE}, headers=h("admin")
    )
    url = "/v1/platform/edge-releases/1.1.0/rollout"
    box = pair_device(client, org, h("owner_a"))
    # A box needs its client; another client's box isn't found.
    assert (
        client.post(url, json={"device_id": box["device_id"]}, headers=h("admin")).status_code
        == 422
    )
    wrong = {"tenant_id": str(org.ids["b"]), "device_id": box["device_id"]}
    assert client.post(url, json=wrong, headers=h("admin")).status_code == 404
    missing = "/v1/platform/edge-releases/9.9.9/rollout"
    assert client.post(missing, json={}, headers=h("admin")).status_code == 404
