"""Handles requests the cloud passes to this box (`cctv_contracts.v1.remote`): live view
and NVR playback through go2rtc, the NVR's recording list, a current frame, and face
embeddings for enrolment.

Each request gets exactly one reply. Errors go back as `ok=False` with a short message
that never contains credentials.
"""

import base64
import logging
import tempfile
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from cctv_contracts.v1 import (
    CameraConfig,
    EdgeConfig,
    FaceEmbeddingRequest,
    FrameRequest,
    LiveRequest,
    PlaybackRequest,
    RecordingsRequest,
    RemoteReply,
    RemoteRequest,
)
from cctv_edge import ffmpeg
from cctv_edge.cloud import CloudClient, CloudError
from cctv_edge.credentials import Credential, redact, with_credentials
from cctv_edge.go2rtc import Go2rtc
from cctv_edge.nvr import playback_url, search_recordings

log = logging.getLogger(__name__)

# Playback streams are per request; go2rtc stops reading them when the viewer leaves, and
# they are removed after this long.
PLAYBACK_STREAM_TTL_SECONDS = 3 * 3600
FRAME_TIMEOUT_SECONDS = 15
FRAME_MAX_WIDTH = 1280


class RemoteRefusedError(Exception):
    """Expected failure with a message that is safe to send to the cloud."""


class RemoteWorker:
    def __init__(
        self,
        client: CloudClient,
        go2rtc: Go2rtc,
        get_config: Callable[[], EdgeConfig | None],
        credentials: Mapping[str, Credential],
        ffmpeg_bin: str,
        workers: int = 4,
        nvr_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client = client
        self.go2rtc = go2rtc
        self.get_config = get_config
        self.credentials = credentials
        self.ffmpeg_bin = ffmpeg_bin
        self.nvr_transport = nvr_transport
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="remote")
        self._playback_streams: dict[str, float] = {}  # stream name -> created (epoch s)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # --- dispatch ---------------------------------------------------------------------

    def submit(self, request: RemoteRequest) -> None:
        self._executor.submit(self.process, request)

    def process(self, request: RemoteRequest, now: float | None = None) -> RemoteReply | None:
        """Handle `request` and send the reply; returns it (None if it had expired)."""
        now_dt = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
        if request.expires_at <= now_dt:
            log.info("remote request %s expired before it was handled; dropped", request.id)
            return None
        try:
            reply = self.handle(request, now_dt.timestamp())
        except RemoteRefusedError as exc:
            reply = RemoteReply(ok=False, error=self._scrub(str(exc)))
        except Exception as exc:
            log.exception("remote request %s (%s) failed", request.id, request.request.kind.value)
            reply = RemoteReply(ok=False, error=self._scrub(f"{type(exc).__name__}: {exc}"))
        try:
            self.client.reply_remote(request.id, reply)
        except CloudError as exc:
            log.warning("could not reply to remote request %s: %s", request.id, exc)
        return reply

    def handle(self, request: RemoteRequest, now: float) -> RemoteReply:
        payload = request.request
        if isinstance(payload, FaceEmbeddingRequest):
            raise RemoteRefusedError("face model not installed on this box yet (Phase 5)")
        config, camera = self._camera(payload.camera_id)
        if isinstance(payload, LiveRequest):
            return self._live(camera, payload)
        if isinstance(payload, PlaybackRequest):
            return self._playback(config, camera, payload, request, now)
        if isinstance(payload, RecordingsRequest):
            return self._recordings(config, camera, payload)
        if isinstance(payload, FrameRequest):
            return self._frame(camera)
        raise RemoteRefusedError("unsupported request")

    # --- kinds ------------------------------------------------------------------------

    def _live(self, camera: CameraConfig, payload: LiveRequest) -> RemoteReply:
        name = f"live-{camera.id}"
        source = self._camera_url(camera, camera.rtsp_sub or camera.rtsp_main)
        self.go2rtc.ensure_stream(name, source)
        return RemoteReply(ok=True, sdp_answer=self.go2rtc.webrtc_answer(name, payload.sdp_offer))

    def _playback(
        self,
        config: EdgeConfig,
        camera: CameraConfig,
        payload: PlaybackRequest,
        request: RemoteRequest,
        now: float,
    ) -> RemoteReply:
        nvr = camera.nvr
        if nvr is None:
            raise RemoteRefusedError("camera has no NVR channel set; playback is not available")
        url = playback_url(nvr, payload.start, payload.end, config.timezone)
        source = with_credentials(
            url, self._credential(nvr.credentials_ref or camera.credentials_ref)
        )
        name = f"pb-{request.id}"
        self.go2rtc.ensure_stream(name, source)
        with self._lock:
            self._playback_streams[name] = now
        return RemoteReply(ok=True, sdp_answer=self.go2rtc.webrtc_answer(name, payload.sdp_offer))

    def _recordings(
        self, config: EdgeConfig, camera: CameraConfig, payload: RecordingsRequest
    ) -> RemoteReply:
        nvr = camera.nvr
        if nvr is None:
            raise RemoteRefusedError("camera has no NVR channel set; recordings are not available")
        credential = self._credential(nvr.credentials_ref or camera.credentials_ref)
        spans = search_recordings(
            nvr,
            credential,
            payload.start,
            payload.end,
            config.timezone,
            transport=self.nvr_transport,
        )
        return RemoteReply(ok=True, recordings=spans)

    def _frame(self, camera: CameraConfig) -> RemoteReply:
        jpeg = self.grab_frame(self._camera_url(camera, camera.rtsp_sub or camera.rtsp_main))
        return RemoteReply(ok=True, jpeg_base64=base64.b64encode(jpeg).decode("ascii"))

    def grab_frame(self, url: str) -> bytes:
        """One JPEG frame from `url` (a camera stream, or a file in tests)."""
        with tempfile.TemporaryDirectory(prefix="cctv-frame-") as tmp:
            out = Path(tmp) / "frame.jpg"
            try:
                ffmpeg.run(
                    self.ffmpeg_bin,
                    [
                        *ffmpeg.input_args(url),
                        *("-frames:v", "1", "-vf", f"scale='min({FRAME_MAX_WIDTH},iw)':-2"),
                        *("-q:v", "4", "-f", "image2", str(out)),
                    ],
                    timeout=FRAME_TIMEOUT_SECONDS,
                )
            except ffmpeg.FfmpegError as exc:
                raise RemoteRefusedError(f"could not read a frame: {exc}") from exc
            if not out.exists():
                raise RemoteRefusedError("could not read a frame: no picture from the camera")
            return out.read_bytes()

    # --- housekeeping -----------------------------------------------------------------

    def cleanup(self, now: float) -> None:
        """Remove playback streams older than PLAYBACK_STREAM_TTL_SECONDS."""
        with self._lock:
            old = [
                name
                for name, created in self._playback_streams.items()
                if now - created > PLAYBACK_STREAM_TTL_SECONDS
            ]
        for name in old:
            try:
                self.go2rtc.delete_stream(name)
            except Exception as exc:
                log.warning("could not remove playback stream %s: %s", name, self._scrub(str(exc)))
                continue
            with self._lock:
                self._playback_streams.pop(name, None)

    @property
    def playback_streams(self) -> list[str]:
        with self._lock:
            return list(self._playback_streams)

    # --- helpers ----------------------------------------------------------------------

    def _camera(self, camera_id) -> tuple[EdgeConfig, CameraConfig]:
        config = self.get_config()
        if config is None:
            raise RemoteRefusedError("this box has no config yet")
        for camera in config.cameras:
            if camera.id == camera_id:
                return config, camera
        raise RemoteRefusedError("camera is not configured on this box")

    def _credential(self, ref: str | None) -> Credential | None:
        if not ref:
            return None
        credential = self.credentials.get(ref)
        if credential is None:
            raise RemoteRefusedError(f"no local credentials named {ref!r} on this box")
        return credential

    def _camera_url(self, camera: CameraConfig, url: str) -> str:
        return with_credentials(url, self._credential(camera.credentials_ref))

    def _scrub(self, text: str) -> str:
        """Remove anything that could be a credential from a message for the cloud."""
        text = redact(text)
        for credential in self.credentials.values():
            for secret in (credential.password, credential.username):
                if len(secret) >= 3:
                    text = text.replace(secret, "***").replace(quote(secret, safe=""), "***")
        return text[:500]
