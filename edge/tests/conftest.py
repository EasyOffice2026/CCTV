import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from cctv_contracts.enums import CameraRole, FeatureKey, ZoneType
from cctv_contracts.v1 import (
    CameraConfig,
    EdgeConfig,
    FeatureConfig,
    WorkingHours,
    ZoneConfig,
)
from cctv_edge.cloud import CloudClient
from cctv_edge.settings import EdgeSettings

CLOUD_URL = "https://cloud.test"


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    return imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    path = find_ffmpeg()
    if path is None:
        pytest.skip("ffmpeg not available")
    return path


@pytest.fixture(scope="session")
def sample_video(ffmpeg_bin, tmp_path_factory) -> Path:
    """10 s, 10 fps H.264 test pattern with a keyframe every second, like a camera stream."""
    path = tmp_path_factory.mktemp("video") / "camera.mp4"
    subprocess.run(  # noqa: S603
        [
            ffmpeg_bin,
            *("-hide_banner", "-loglevel", "error", "-f", "lavfi"),
            *("-i", "testsrc=size=320x240:rate=10", "-t", "10"),
            *("-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(path)),
        ],
        check=True,
    )
    return path


class FakeCloud:
    """In-memory stand-in for the cloud API and object storage."""

    def __init__(self) -> None:
        self.down = False
        self.events: dict[str, dict] = {}
        self.event_posts: list[list[dict]] = []
        self.objects: dict[str, bytes] = {}
        self.heartbeats: list[dict] = []
        self.config: EdgeConfig | None = None
        self.reject: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("network is down", request=request)
        path = request.url.path
        if request.url.host == "storage.test":
            assert "authorization" not in request.headers  # device token must not leak
            self.objects[path.lstrip("/")] = request.content
            return httpx.Response(200)
        assert request.headers["authorization"] == "Bearer test-token"
        if path == "/v1/edge/media/upload-urls":
            body = json.loads(request.content)
            ext = "jpg" if body["kind"] == "snapshot" else "mp4"
            key = f"tenant/{body['event_id']}/{body['kind']}.{ext}"
            return httpx.Response(
                200,
                json={
                    "media_key": key,
                    "upload_url": f"https://storage.test/{key}?signature=secret",
                    "expires_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        if path == "/v1/edge/events":
            events = json.loads(request.content)["events"]
            self.event_posts.append(events)
            results = []
            for event in events:
                rejected = event["event_id"] in self.reject
                if not rejected:
                    self.events[event["event_id"]] = event
                results.append(
                    {
                        "event_id": event["event_id"],
                        "accepted": not rejected,
                        "error": "bad" if rejected else None,
                    }
                )
            return httpx.Response(200, json={"results": results})
        if path == "/v1/edge/heartbeat":
            self.heartbeats.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "server_time": datetime.now(timezone.utc).isoformat(),
                    "latest_config_version": self.config.version if self.config else 0,
                },
            )
        if path == "/v1/edge/config":
            if self.config is None or request.headers.get("if-none-match") == (
                f'"{self.config.version}"'
            ):
                return httpx.Response(304)
            return httpx.Response(200, content=self.config.model_dump_json())
        return httpx.Response(404)


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


@pytest.fixture
def client(cloud: FakeCloud):
    c = CloudClient(CLOUD_URL, "test-token", transport=httpx.MockTransport(cloud.handler))
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path: Path) -> EdgeSettings:
    return EdgeSettings(
        _env_file=None,
        data_dir=tmp_path / "data",
        ffmpeg=find_ffmpeg() or "ffmpeg",
        segment_seconds=2,
    )


def zone(zone_type: ZoneType, points: list[tuple[float, float]]) -> ZoneConfig:
    return ZoneConfig(id=uuid.uuid4(), name=zone_type.value, type=zone_type, points=points)


def camera(
    features: list[FeatureKey | FeatureConfig] = (),
    zones: list[ZoneConfig] = (),
    **kwargs,
) -> CameraConfig:
    return CameraConfig(
        id=kwargs.pop("id", uuid.uuid4()),
        name=kwargs.pop("name", "kitchen-1"),
        role=CameraRole.KITCHEN,
        rtsp_main=kwargs.pop("rtsp_main", "rtsp://nvr.local:554/Streaming/Channels/101"),
        rtsp_sub=kwargs.pop("rtsp_sub", "rtsp://nvr.local:554/Streaming/Channels/102"),
        features=[f if isinstance(f, FeatureConfig) else FeatureConfig(key=f) for f in features],
        zones=list(zones),
        **kwargs,
    )


def edge_config(
    cameras: list[CameraConfig], *, version: int = 1, closed_all_day: bool = True
) -> EdgeConfig:
    """By default the branch is open only one hour, three days from today, so it is
    after hours whenever the test runs."""
    hours = []
    if closed_all_day:
        weekday = (datetime.now(timezone.utc).weekday() + 3) % 7
        hours = [WorkingHours(weekday=weekday, opens_at="09:00", closes_at="10:00")]
    return EdgeConfig(
        version=version,
        generated_at=datetime.now(timezone.utc),
        tenant_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        working_hours=hours,
        cameras=cameras,
    )
