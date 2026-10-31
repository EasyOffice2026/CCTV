"""Convert a Label Studio export of event intervals into the ground-truth CSV.

    python -m cctv_ml.labelstudio --export export.json --videos videos.csv --out gt.csv

- export: Label Studio "JSON" export of a project using
  `labelling/configs/event_intervals.xml` (TimelineLabels on a video).
- videos CSV: `video, camera_id, started_at, fps`. `video` is the file name of the task's
  video (Label Studio's upload prefix such as `1a2b3c4d-` is ignored when matching),
  `started_at` the real time of the first frame (ISO-8601 with offset; take it from the
  NVR export or the replay inventory, never from the overlay on the picture), `fps` the
  frame rate given to the `<Video frameRate>` tag.

Timeline ranges are frame numbers. Frame `--frame-base` (1 by default, as in Label Studio
1.x) is the first frame, and the end frame is included: a range 26-50 at 25 fps on a
video starting 12:00:00 becomes 12:00:01 to 12:00:02. Check this once against a label
you placed by hand when Label Studio is upgraded. Use `--units seconds` if the ranges
are already in seconds.

Per task, the latest annotation that was not skipped is used.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from cctv_contracts.enums import EventType
from cctv_ml.io import InputError, parse_time, read_csv, safe_console, write_csv

GT_COLUMNS = ("camera_id", "event_type", "started_at", "ended_at", "note")
_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{8}-")


@dataclass(frozen=True)
class Video:
    camera_id: str
    started_at: datetime
    fps: float


def video_name(value: str) -> str:
    """File name of a task's video, without Label Studio's upload prefix."""
    name = PurePosixPath(unquote(urlparse(value).path or value)).name
    return _UPLOAD_PREFIX.sub("", name)


def load_videos(path: Path) -> dict[str, Video]:
    videos: dict[str, Video] = {}
    for line, row in read_csv(path, ("video", "camera_id", "started_at", "fps")):
        where = f"{path}:{line}"
        try:
            fps = float(row["fps"])
        except ValueError as exc:
            raise InputError(f"{where}: fps is not a number") from exc
        if fps <= 0:
            raise InputError(f"{where}: fps must be positive")
        name = video_name(row["video"])
        if name in videos:
            raise InputError(f"{where}: video {name} is listed twice")
        videos[name] = Video(row["camera_id"], parse_time(row["started_at"], where), fps)
    return videos


def _annotation(task: dict[str, Any]) -> dict[str, Any] | None:
    annotations = [a for a in task.get("annotations") or [] if not a.get("was_cancelled")]
    if not annotations:
        return None
    return max(annotations, key=lambda a: (a.get("updated_at") or "", a.get("id") or 0))


def convert(
    tasks: list[dict[str, Any]],
    videos: dict[str, Video],
    data_key: str = "video",
    frame_base: int = 1,
    units: str = "frames",
) -> tuple[list[dict[str, str]], list[str]]:
    """Ground-truth rows and warnings (tasks skipped)."""
    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    for task in tasks:
        task_id = task.get("id", "?")
        source = (task.get("data") or {}).get(data_key)
        if not source:
            warnings.append(f"task {task_id}: no data.{data_key}; skipped")
            continue
        video = videos.get(video_name(str(source)))
        if video is None:
            warnings.append(f"task {task_id}: {video_name(str(source))} not in videos CSV")
            continue
        annotation = _annotation(task)
        if annotation is None:
            warnings.append(f"task {task_id}: not annotated; skipped")
            continue
        for result in annotation.get("result") or []:
            if result.get("type") != "timelinelabels":
                continue
            value = result.get("value") or {}
            for label in value.get("timelinelabels") or []:
                try:
                    event_type = EventType(label)
                except ValueError as exc:
                    raise InputError(f"task {task_id}: unknown event type {label!r}") from exc
                for span in value.get("ranges") or []:
                    start, end = float(span["start"]), float(span["end"])
                    if units == "frames":
                        start_s = (start - frame_base) / video.fps
                        end_s = (end - frame_base + 1) / video.fps
                    else:
                        start_s, end_s = start, end
                    if end_s < start_s:
                        raise InputError(f"task {task_id}: range ends before it starts")
                    rows.append(
                        {
                            "camera_id": video.camera_id,
                            "event_type": event_type.value,
                            "started_at": (
                                video.started_at + timedelta(seconds=start_s)
                            ).isoformat(),
                            "ended_at": (video.started_at + timedelta(seconds=end_s)).isoformat(),
                            "note": f"ls task {task_id}",
                        }
                    )
    rows.sort(key=lambda r: (r["camera_id"], r["started_at"], r["event_type"]))
    return rows, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cctv_ml.labelstudio", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--export", type=Path, required=True, help="Label Studio JSON export")
    parser.add_argument("--videos", type=Path, required=True, help="video -> camera/time CSV")
    parser.add_argument("--out", type=Path, required=True, help="ground-truth CSV to write")
    parser.add_argument("--data-key", default="video", help="task data field of the video")
    parser.add_argument("--frame-base", type=int, default=1, help="number of the first frame")
    parser.add_argument("--units", choices=("frames", "seconds"), default="frames")
    args = parser.parse_args(argv)
    safe_console()
    try:
        tasks = json.loads(args.export.read_text(encoding="utf-8"))
        if not isinstance(tasks, list):
            raise InputError(f"{args.export}: expected a list of tasks (JSON export)")
        rows, warnings = convert(
            tasks, load_videos(args.videos), args.data_key, args.frame_base, args.units
        )
    except (InputError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    write_csv(args.out, GT_COLUMNS, rows)
    print(f"wrote {len(rows)} labelled events to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
