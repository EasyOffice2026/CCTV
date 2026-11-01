"""The real edge client (cctv_edge) against the real cloud API, in one process.

Proves the two sides agree on the contract: pairing, config pull, media upload, event
sync (open, then closed with a clip), heartbeat, face templates, sightings and counts.
"""

import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.orm import Session

from cctv_cloud.db.models import FaceTemplate
from cctv_cloud.services.face_keys import seal_embedding
from tests.conftest import login

cctv_edge = pytest.importorskip("cctv_edge")

from cctv_contracts.v1 import (  # noqa: E402
    CameraStatus,
    CountBatch,
    CountBucketIn,
    Heartbeat,
    SightingBatch,
    StaffSighting,
    SystemStats,
    UpdateReport,
)
from cctv_edge.cloud import CloudClient  # noqa: E402
from cctv_edge.outbox import Outbox  # noqa: E402
from cctv_edge.sync import Syncer  # noqa: E402


class Router(httpx.BaseTransport):
    """API calls go to the app; PUTs to the fake storage host are kept in memory."""

    def __init__(self, app_transport: httpx.BaseTransport) -> None:
        self.app_transport = app_transport
        self.objects: dict[str, bytes] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.test":
            request.read()
            self.objects[request.url.path.lstrip("/")] = request.content
            return httpx.Response(200)
        return self.app_transport.handle_request(request)


def test_edge_and_cloud_work_together(client, org, tmp_path):
    owner = login(client, org, "owner_a")
    registered = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids["a1"]), "name": "Kitchen box", "serial": "JETSON-1"},
        headers=owner,
    ).json()
    paired = client.post("/v1/edge/pair", json={"pairing_code": registered["pairing_code"]}).json()
    client.put(
        "/v1/feature-settings",
        json={
            "scope": "branch",
            "branch_id": str(org.ids["a1"]),
            "feature_key": "security",
            "enabled": True,
        },
        headers=owner,
    )

    router = Router(client._transport)
    edge = CloudClient(str(client.base_url), paired["device_token"], transport=router)
    try:
        config = edge.fetch_config(None)
        assert config is not None
        assert [f.key.value for f in config.cameras[0].features] == ["security"]
        assert edge.fetch_config(config.version) is None  # unchanged: 304

        # The edge records an event the way the service does, then syncs it.
        outbox = Outbox(tmp_path / "outbox.sqlite3")
        event_id = uuid.uuid4()
        now = time.time()
        snapshot = tmp_path / "snapshot.jpg"
        snapshot.write_bytes(b"\xff\xd8 fake jpeg")
        outbox.open_event(
            {
                "event_id": str(event_id),
                "camera_id": str(config.cameras[0].id),
                "zone_id": None,
                "feature": "security",
                "event_type": "after_hours_person",
                "severity": "critical",
                "status": "open",
                "started_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "confidence": 0.91,
                "track_id": None,
                "attributes": {},
            },
            snapshot,
            now,
        )
        syncer = Syncer(outbox, edge)
        assert syncer.sync_once(now) == 1

        outbox.end_event(event_id, now + 8, now, now)
        [job] = outbox.clips_due(now)
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake mp4")
        outbox.close_event(job.event_id, clip, {"duration_s": 8.0}, now)
        assert syncer.sync_once(now) == 1
        assert outbox.pending_count() == 0
        outbox.close()

        beat = edge.heartbeat(
            Heartbeat(
                sent_at=datetime.now(timezone.utc),
                software_version=cctv_edge.__version__,
                config_version_applied=config.version,
                uptime_seconds=10,
                cameras=[CameraStatus(camera_id=config.cameras[0].id, health="online", fps=5.0)],
                system=SystemStats(cpu_percent=10, disk_free_bytes=10**9, outbox_pending=0),
            )
        )
        assert beat.latest_config_version == config.version
    finally:
        edge.close()

    stored = client.get(f"/v1/events/{event_id}", headers=owner).json()
    assert stored["status"] == "closed"
    assert stored["has_snapshot"] and stored["has_clip"]
    assert stored["attributes"] == {"duration_s": 8.0}
    assert sorted(router.objects.values()) == sorted([b"\xff\xd8 fake jpeg", clip.read_bytes()])
    camera = client.get(f"/v1/cameras/{org.ids['cam_a1']}", headers=owner).json()
    assert camera["health"] == "online"


def test_attendance_counts_and_templates_through_the_edge_client(client, org, owner_engine):
    """Phase 5 endpoints: face templates (with ETag), sightings and count buckets."""
    owner = login(client, org, "owner_a")
    device = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids["a1"]), "name": "Door box", "serial": "JETSON-2"},
        headers=owner,
    ).json()
    paired = client.post("/v1/edge/pair", json={"pairing_code": device["pairing_code"]}).json()
    staff = client.post(
        "/v1/staff",
        json={"branch_id": str(org.ids["a1"]), "employee_code": "E1", "full_name": "Sara"},
        headers=owner,
    ).json()
    signed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    client.put(f"/v1/staff/{staff['id']}/consent", json={"signed_at": signed}, headers=owner)
    sealed, key_id = seal_embedding([0.125] * 128, org.ids["a"], uuid.UUID(staff["id"]))
    with Session(owner_engine) as s, s.begin():
        s.add(
            FaceTemplate(
                tenant_id=org.ids["a"],
                staff_id=uuid.UUID(staff["id"]),
                embedding_encrypted=sealed,
                key_id=key_id,
                model_version="sface-test",
            )
        )

    edge = CloudClient(str(client.base_url), paired["device_token"], transport=client._transport)
    try:
        fetched = edge.fetch_face_templates(None)
        assert fetched is not None
        templates, etag = fetched
        assert [str(t.staff_id) for t in templates.templates] == [staff["id"]]
        assert templates.templates[0].embedding == pytest.approx([0.125] * 128)
        assert edge.fetch_face_templates(etag) is None  # unchanged: 304

        camera = org.ids["cam_a1"]
        seen = datetime.now(timezone.utc) - timedelta(hours=1)
        result = edge.send_sightings(
            SightingBatch(
                sightings=[
                    StaffSighting(
                        staff_id=uuid.UUID(staff["id"]),
                        camera_id=camera,
                        seen_at=seen,
                        similarity=0.7,
                    ),
                    StaffSighting(
                        staff_id=uuid.uuid4(), camera_id=camera, seen_at=seen, similarity=0.7
                    ),
                ]
            )
        )
        assert result.accepted == 1 and [r.index for r in result.rejected] == [1]

        bucket = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
        counted = edge.send_counts(
            CountBatch(buckets=[CountBucketIn(camera_id=camera, bucket_start=bucket, in_count=3)])
        )
        assert counted.accepted == 1 and counted.rejected == []
    finally:
        edge.close()


def test_software_update_round_trip(client, org, tmp_path):
    """Plan 2.10: a rollout reaches the box's updater through the edge client, and the
    updater's reports come back to the cloud. Docker is a stand-in that always succeeds."""
    from cctv_edge import updater

    owner, admin = login(client, org, "owner_a"), login(client, org, "admin")
    device = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids["a1"]), "name": "Update box", "serial": "JETSON-3"},
        headers=owner,
    ).json()
    paired = client.post("/v1/edge/pair", json={"pairing_code": device["pairing_code"]}).json()
    image = "ghcr.io/easyoffice2026/cctv-edge@sha256:" + "c" * 64
    client.post(
        "/v1/platform/edge-releases", json={"version": "1.1.0", "image": image}, headers=admin
    )
    client.post(
        "/v1/platform/edge-releases/1.1.0/rollout",
        json={"tenant_id": str(org.ids["a"]), "device_id": paired["device_id"]},
        headers=admin,
    )

    data, conf, install = tmp_path / "data", tmp_path / "conf", tmp_path / "opt"
    for folder in (data / "update", conf, install):
        folder.mkdir(parents=True)
    (conf / "edge.env").write_text("CCTV_EDGE_IMAGE=ghcr.io/easyoffice2026/cctv-edge:0.1.0\n")
    clock = [datetime.now(timezone.utc)]

    def docker(args, env):
        out = "0\n" if args[1] == "inspect" else "id\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    box = updater.Updater(data, conf, install, runner=docker, clock=lambda: clock[0])

    edge = CloudClient(str(client.base_url), paired["device_token"], transport=client._transport)

    def heartbeat(version: str):
        status = updater.read_status(box.update_dir)
        return edge.heartbeat(
            Heartbeat(
                sent_at=datetime.now(timezone.utc),
                software_version=version,
                config_version_applied=1,
                uptime_seconds=10,
                cameras=[],
                system=SystemStats(cpu_percent=10, disk_free_bytes=10**9, outbox_pending=0),
                update=UpdateReport.model_validate(status) if status else None,
            )
        )

    try:
        offer = heartbeat("0.1.0").update
        assert offer is not None and offer.image == image
        updater.request_update(box.update_dir, offer.version, offer.image, offer.requested_at)
        assert box.run_once() == "updating"
        assert heartbeat("0.1.0").update is not None  # still offered while it switches
        [row] = client.get("/v1/edge-devices", headers=owner).json()
        assert (row["target_version"], row["update_state"]) == ("1.1.0", "updating")

        # The new version reports in for a few minutes; the updater confirms it.
        clock[0] += timedelta(minutes=3)
        updater.mark_running(box.update_dir, "1.1.0", clock[0])
        assert box.run_once() == "updated"
        assert heartbeat("1.1.0").update is None
    finally:
        edge.close()
    [row] = client.get("/v1/edge-devices", headers=owner).json()
    assert (row["software_version"], row["update_state"]) == ("1.1.0", "updated")
    [release] = client.get("/v1/platform/edge-releases", headers=admin).json()
    assert (release["running"], release["updating"]) == (1, 0)


def test_heatmaps_through_the_edge_client(client, org, tmp_path):
    """Later phase: the box's heatmap buckets, from its outbox to the dashboard's grid."""
    from cctv_contracts.v1 import HeatmapBucketIn

    owner = login(client, org, "owner_a")
    device = client.post(
        "/v1/edge-devices",
        json={"branch_id": str(org.ids["a1"]), "name": "Hall box", "serial": "JETSON-4"},
        headers=owner,
    ).json()
    paired = client.post("/v1/edge/pair", json={"pairing_code": device["pairing_code"]}).json()
    edge = CloudClient(str(client.base_url), paired["device_token"], transport=client._transport)
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    hour = datetime(2026, 10, 31, 9, tzinfo=timezone.utc)
    try:
        outbox.add_heatmaps(
            [HeatmapBucketIn(camera_id=org.ids["cam_a1"], bucket_start=hour, cells=[(40, 300)])]
        )
        assert Syncer(outbox, edge).sync_heatmaps(time.time()) == 1
    finally:
        outbox.close()
        edge.close()
    params = {
        "camera_id": str(org.ids["cam_a1"]),
        "from": hour.isoformat(),
        "to": (hour + timedelta(hours=1)).isoformat(),
    }
    grid = client.get("/v1/heatmaps", params=params, headers=owner).json()
    assert (grid["columns"], grid["rows"], grid["seconds"][40], sum(grid["seconds"])) == (
        32,
        18,
        300,
        300,
    )
