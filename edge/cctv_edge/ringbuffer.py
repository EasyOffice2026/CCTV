"""Ring buffer: the last ~45 s of each camera's main stream as short MPEG-TS segments.

ffmpeg copies the stream without re-encoding (cheap) and names each segment after its UTC
start time. A clip can then include footage from before the event started.
"""

import calendar
import itertools
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cctv_edge import ffmpeg
from cctv_edge.credentials import redact

log = logging.getLogger(__name__)

NAME_FORMAT = "%Y%m%dT%H%M%SZ"
SUFFIX = ".ts"


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float


def list_segments(directory: Path) -> list[Segment]:
    segments = []
    for path in directory.glob(f"*{SUFFIX}"):
        try:
            start = calendar.timegm(time.strptime(path.stem, NAME_FORMAT))
        except ValueError:
            continue
        segments.append(Segment(path, float(start)))
    return sorted(segments, key=lambda s: s.start)


def segments_between(segments: Sequence[Segment], start: float, end: float) -> list[Segment]:
    """Finished segments overlapping [start, end]. Segment i runs until segment i+1 starts;
    the newest one is still being written and is never included."""
    return [
        current
        for current, following in itertools.pairwise(segments)
        if current.start <= end and following.start > start
    ]


def prune(directory: Path, keep_seconds: float, now: float) -> int:
    """Delete segments that ended more than `keep_seconds` ago."""
    segments = list_segments(directory)
    removed = 0
    for current, following in itertools.pairwise(segments):
        if following.start < now - keep_seconds:
            current.path.unlink(missing_ok=True)
            removed += 1
    return removed


class SegmentRecorder:
    """Keeps an ffmpeg segmenter running for one camera, restarting it with backoff."""

    def __init__(
        self,
        ffmpeg_bin: str,
        url: str,
        directory: Path,
        segment_seconds: int,
        keep_seconds: float,
        extra_input_args: Sequence[str] = (),
    ) -> None:
        self.ffmpeg_bin = ffmpeg_bin
        self.url = url
        self.directory = directory
        self.segment_seconds = segment_seconds
        self.keep_seconds = keep_seconds
        self.extra_input_args = extra_input_args
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def args(self) -> list[str]:
        return [
            *ffmpeg.input_args(self.url, self.extra_input_args),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_seconds),
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(self.directory / f"{NAME_FORMAT}{SUFFIX}"),
        ]

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name=f"ring-{self.directory.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            process = ffmpeg.spawn(self.ffmpeg_bin, self.args())
            stderr = ffmpeg.drain_stderr(process)
            started = time.monotonic()
            while process.poll() is None and not self._stop.wait(1):
                prune(self.directory, self.keep_seconds, time.time())
            ffmpeg.stop(process)
            if self._stop.is_set():
                break
            self.last_error = stderr[-1] if stderr else f"ffmpeg exited ({process.returncode})"
            log.warning("ring buffer %s stopped: %s", redact(self.url), self.last_error)
            backoff = 1.0 if time.monotonic() - started > 60 else min(backoff * 2, 30)
            self._stop.wait(backoff)
