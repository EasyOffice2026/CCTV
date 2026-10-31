"""The real edge client (cctv_edge) against the real cloud API, in one process.

Proves the two sides agree on the contract: pairing, config pull, media upload, event
sync (open, then closed with a clip) and heartbeat.
"""

import time
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from tests.conftest import login

cctv_edge = pytest.importorskip("cctv_edge")

from cctv_contracts.v1 import CameraStatus, Heartbeat, SystemStats  # noqa: E402
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
