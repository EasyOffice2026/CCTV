"""Helpers for running ffmpeg as a child process."""

import os
import subprocess
import threading
from collections import deque
from collections.abc import Sequence
from typing import IO

from cctv_edge.credentials import redact

# Segment and clip file names use UTC; TZ makes ffmpeg's strftime use it too.
FFMPEG_ENV = {**os.environ, "TZ": "UTC0"}


class FfmpegError(RuntimeError):
    pass


def input_args(url: str, extra: Sequence[str] = ()) -> list[str]:
    """Arguments to open a camera stream (or, in tests, a file)."""
    args = list(extra)
    if url.startswith(("rtsp://", "rtsps://")):
        # TCP avoids smeared frames from UDP loss; the timeout (µs) detects dead cameras.
        args += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
    return [*args, "-i", url]


def spawn(ffmpeg: str, args: Sequence[str], stdout: int | None = None) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - fixed binary, arguments are not shell-parsed
        [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *args],
        stdout=stdout if stdout is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=FFMPEG_ENV,
    )


def drain_stderr(process: subprocess.Popen, lines: int = 20) -> deque[str]:
    """Collect the last stderr lines in the background so the pipe never fills up."""
    tail: deque[str] = deque(maxlen=lines)

    def _read(stream: IO[bytes]) -> None:
        for raw in stream:
            tail.append(redact(raw.decode(errors="replace").rstrip()))

    if process.stderr is not None:
        threading.Thread(target=_read, args=(process.stderr,), daemon=True).start()
    return tail


def run(ffmpeg: str, args: Sequence[str], timeout: float) -> None:
    try:
        result = subprocess.run(  # noqa: S603
            [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
            capture_output=True,
            timeout=timeout,
            env=FFMPEG_ENV,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    if result.returncode != 0:
        message = redact(result.stderr.decode(errors="replace").strip()[-500:])
        raise FfmpegError(message or f"ffmpeg exited with {result.returncode}")


def stop(process: subprocess.Popen, timeout: float = 5) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
