from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cctv_edge import __version__


class EdgeSettings(BaseSettings):
    """Device-local settings. Everything per-branch (cameras, zones, features) comes from
    the cloud config instead; see `cctv_contracts.v1.EdgeConfig`."""

    model_config = SettingsConfigDict(env_prefix="CCTV_EDGE_", env_file=".env", extra="ignore")

    # https:// (plan 7.1); plain http only to this machine, or with allow_insecure_cloud
    # for a development cloud elsewhere on the LAN.
    cloud_url: str = "http://localhost:8000"
    allow_insecure_cloud: bool = False
    device_token: SecretStr = SecretStr("")
    http_timeout_seconds: float = 10

    # The release this image was built as (set by the release workflow; plan 2.10).
    version: str = __version__

    data_dir: Path = Path("/var/lib/cctv-edge")
    # JSON file {credentials_ref: {"username": ..., "password": ...}}, readable by the service only.
    credentials_file: Path = Path("/etc/cctv-edge/credentials.json")
    ffmpeg: str = "ffmpeg"

    # Analysis stream: decoded at a low fps and resolution for detection.
    analysis_fps: float = Field(default=5, gt=0, le=30)
    analysis_width: int = 640
    analysis_height: int = 360
    # A camera is offline after this long without frames.
    stale_after_seconds: float = 10

    # Ring buffer of the main stream that clips are cut from. It must cover the longest
    # event plus pre/post-roll (up to 30 s + 60 s) and the time to cut the clip.
    segment_seconds: int = Field(default=2, ge=1, le=10)
    ring_seconds: int = Field(default=240, ge=60, le=1800)
    # Events longer than this are closed and a new one starts, so clips stay small.
    max_event_seconds: int = Field(default=120, ge=10, le=600)

    # Local media (snapshots, clips) waiting for or already uploaded to the cloud.
    disk_quota_bytes: int = 20 * 1024**3
    keep_sent_media_hours: float = 24

    sync_interval_seconds: float = 2
    config_poll_seconds: float = 60
    heartbeat_seconds: float = 30

    # Requests from the cloud (live view, playback, recordings, frames): long poll length
    # and how many are handled at once. go2rtc serves the WebRTC streams; its API must
    # only listen on localhost (see infra/edge/go2rtc.yaml).
    go2rtc_url: str = "http://127.0.0.1:1984"
    remote_wait_seconds: int = Field(default=25, ge=1, le=55)
    remote_workers: int = Field(default=4, ge=1, le=16)

    # Built-in detectors to load ("motion" needs no weights). Model-based detectors and
    # classifiers are found in models_dir (one folder per model with a model.json).
    detectors: list[str] = Field(default_factory=lambda: ["motion"])
    models_dir: Path | None = None  # default: <data_dir>/models
    # ONNX Runtime providers in order of preference; unavailable ones are skipped.
    model_providers: list[str] = Field(
        default_factory=lambda: [
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
    )
    # Cameras running kitchen compliance or attendance are analysed at this larger size,
    # so gloves and faces have enough pixels. Their sub-stream should be at least 720p.
    detail_width: int = 1280
    detail_height: int = 720
    # Face templates are refetched this often (and whenever the config version changes).
    face_templates_seconds: float = Field(default=300, ge=30)

    @field_validator("version")
    @classmethod
    def _default_version(cls, value: str) -> str:
        return value or __version__

    @model_validator(mode="after")
    def _check_ring(self) -> "EdgeSettings":
        if self.ring_seconds < self.max_event_seconds + 100:
            raise ValueError("ring_seconds must be at least max_event_seconds + 100")
        return self

    @model_validator(mode="after")
    def _check_tls(self) -> "EdgeSettings":
        # The device token and every event travel on this connection.
        url = urlsplit(self.cloud_url)
        local = url.hostname in ("localhost", "127.0.0.1", "::1")
        if url.scheme != "https" and not (local or self.allow_insecure_cloud):
            raise ValueError("cloud_url must be https:// (or set allow_insecure_cloud in dev)")
        return self

    @property
    def token_path(self) -> Path:
        return self.data_dir / "device_token"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def ring_dir(self) -> Path:
        return self.data_dir / "ring"

    @property
    def models_path(self) -> Path:
        return self.models_dir or self.data_dir / "models"

    @property
    def outbox_path(self) -> Path:
        return self.data_dir / "outbox.sqlite3"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def alive_path(self) -> Path:
        return self.data_dir / "alive"

    @property
    def update_dir(self) -> Path:
        """Shared with the box's updater (cctv_edge.updater)."""
        return self.data_dir / "update"


@lru_cache
def get_settings() -> EdgeSettings:
    return EdgeSettings()
