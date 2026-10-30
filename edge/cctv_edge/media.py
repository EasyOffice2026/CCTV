"""Event media: JPEG snapshot with boxes drawn, and H.264 MP4 clip from the ring buffer."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from cctv_edge import ffmpeg
from cctv_edge.geometry import Box
from cctv_edge.ringbuffer import Segment

BOX_COLOUR = (255, 40, 40)


def save_snapshot(frame: np.ndarray, boxes: Sequence[Box], path: Path, quality: int = 85) -> None:
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image)
    w, h = image.size
    width = max(2, w // 320)
    for box in boxes:
        draw.rectangle(
            (box.x1 * w, box.y1 * h, box.x2 * w, box.y2 * h), outline=BOX_COLOUR, width=width
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=quality)


# Browser-playable H.264, at most 1280 px wide, no audio, index at the front for streaming.
_ENCODE = [
    "-an",
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-crf",
    "26",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
]


def build_clip(
    ffmpeg_bin: str,
    segments: Sequence[Segment],
    start: float,
    end: float,
    path: Path,
    timeout: float = 180,
) -> None:
    """Cut [start, end] (unix time) out of consecutive ring-buffer segments."""
    if not segments:
        raise ffmpeg.FfmpegError("no ring-buffer segments cover the event")
    path.parent.mkdir(parents=True, exist_ok=True)
    playlist = path.with_suffix(".txt")
    lines = []
    for segment in segments:
        quoted = segment.path.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{quoted}'")
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    offset = max(0.0, start - segments[0].start)
    duration = max(1.0, end - max(start, segments[0].start))
    try:
        ffmpeg.run(
            ffmpeg_bin,
            [
                *("-f", "concat", "-safe", "0", "-i", str(playlist)),
                *("-ss", f"{offset:.2f}", "-t", f"{duration:.2f}"),
                *("-vf", "scale='min(1280,iw)':-2"),
                *_ENCODE,
                str(path),
            ],
            timeout,
        )
    finally:
        playlist.unlink(missing_ok=True)


def still_clip(ffmpeg_bin: str, image: Path, path: Path, seconds: int = 2) -> None:
    """Fallback clip made from the snapshot, when the ring buffer has no footage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        ffmpeg_bin,
        [
            *("-loop", "1", "-i", str(image), "-t", str(seconds), "-r", "5"),
            *("-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"),
            *_ENCODE,
            str(path),
        ],
        timeout=60,
    )
