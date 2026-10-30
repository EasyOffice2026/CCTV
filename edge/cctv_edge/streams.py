"""Stream ingest: decode a camera stream into RGB frames for analysis, and track its health.

The AI reads the sub-stream (low resolution) when the camera has one; otherwise the main
stream, decoded at the analysis fps. ffmpeg decodes on the CPU here; on the Jetson this
moves to the hardware decoder (DeepStream/GStreamer) behind the same `frames()` interface.
"""

import subprocess
import time
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from cctv_contracts.enums import CameraHealth
from cctv_edge import ffmpeg


class FrameReader:
    def __init__(
        self,
        ffmpeg_bin: str,
        url: str,
        fps: float,
        width: int,
        height: int,
        extra_input_args: Sequence[str] = (),
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.url = url
        self.fps = fps
        self.width = width
        self.height = height
        self.extra_input_args = extra_input_args
        self._process: subprocess.Popen | None = None
        self.stderr_tail: deque[str] = deque()

    def frames(self) -> Iterator[tuple[float, np.ndarray]]:
        """Yields (unix time, H x W x 3 RGB frame) until the stream ends or `stop()`."""
        args = [
            *ffmpeg.input_args(self.url, self.extra_input_args),
            "-an",
            "-vf",
            f"fps={self.fps},scale={self.width}:{self.height}",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
        process = self._process = ffmpeg.spawn(self.ffmpeg_bin, args, stdout=subprocess.PIPE)
        self.stderr_tail = ffmpeg.drain_stderr(process)
        size = self.width * self.height * 3
        assert process.stdout is not None  # noqa: S101
        try:
            while True:
                data = process.stdout.read(size)
                if len(data) < size:
                    return
                frame = np.frombuffer(data, np.uint8).reshape(self.height, self.width, 3)
                yield time.time(), frame
        finally:
            ffmpeg.stop(process)

    def stop(self) -> None:
        if self._process is not None:
            ffmpeg.stop(self._process)


@dataclass(frozen=True)
class HealthReport:
    health: CameraHealth
    fps: float
    last_frame_at: datetime | None
    error: str | None


class StreamHealth:
    """Online / degraded (below 60% of the target fps) / offline (no frames recently)."""

    def __init__(self, target_fps: float, stale_after: float = 10, window: float = 10) -> None:
        self.target_fps = target_fps
        self.stale_after = stale_after
        self.window = window
        self._frames: deque[float] = deque()
        self.last_error: str | None = None

    def frame(self, ts: float) -> None:
        self._frames.append(ts)
        self.last_error = None

    def error(self, message: str) -> None:
        self.last_error = message[:500]

    def report(self, now: float) -> HealthReport:
        while self._frames and self._frames[0] < now - self.window:
            self._frames.popleft()
        last = self._frames[-1] if self._frames else None
        if last is None or now - last > self.stale_after:
            health, fps = CameraHealth.OFFLINE, 0.0
        else:
            span = min(self.window, max(now - self._frames[0], 1e-6))
            fps = round(min(len(self._frames) / span, self.target_fps * 2), 2)
            health = CameraHealth.ONLINE if fps >= 0.6 * self.target_fps else CameraHealth.DEGRADED
        last_at = datetime.fromtimestamp(last, timezone.utc) if last is not None else None
        return HealthReport(health, fps, last_at, self.last_error)
