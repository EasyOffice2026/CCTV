"""Offline evaluation: run a camera's real pipeline over a recorded video file.

    python -m cctv_edge.replay kitchen.mp4 --camera camera.json \\
        --start 2026-11-02T21:00:00+03:00 --hours hours.json --out events.jsonl \\
        --coverage coverage.csv

`camera.json` is a `CameraConfig` (features, params and zones as in the cloud config),
`hours.json` a list of `WorkingHours`. Frame times are `--start` plus the time in the
video, so working-hours rules behave as they would have live. Models come from
`--models-dir` (default: the box's models folder).

Each closed event is written as one JSON line:
    {"event_id", "camera_id", "feature", "event_type", "zone_id", "track_id",
     "started_at", "ended_at", "confidence", "attributes"}   (times ISO-8601 UTC)
`--coverage` appends `camera_id,started_at,ended_at` for the replayed span, so the
evaluation knows which footage was covered. Both are read by ml/ (see ml/README.md).

Attendance is not replayed: it needs enrolled templates and is evaluated separately.
"""

import argparse
import csv
import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import TypeAdapter

from cctv_contracts.v1 import CameraConfig, WorkingHours
from cctv_edge.detection import BUILTIN_DETECTORS, DetectorFactory
from cctv_edge.models.classifier import ClassifierFactory
from cctv_edge.models.manifest import ModelTask
from cctv_edge.models.registry import ModelRegistry
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.rules import EventClosed, EventOpened, RuleOutput
from cctv_edge.schedule import BranchSchedule
from cctv_edge.settings import EdgeSettings
from cctv_edge.streams import FrameReader

log = logging.getLogger("cctv_edge.replay")


@dataclass
class ReplayResult:
    events: list[dict]
    started_at: datetime
    ended_at: datetime
    frames: int


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def replay(
    video: Path,
    camera: CameraConfig,
    start: datetime,
    schedule: BranchSchedule,
    detector_factories: Mapping[str, DetectorFactory],
    classifiers: Mapping[ModelTask, ClassifierFactory],
    *,
    ffmpeg: str,
    fps: float,
    size: tuple[int, int] | None = None,
    detail_size: tuple[int, int] = (1280, 720),
    analysis_size: tuple[int, int] = (640, 360),
    max_event_seconds: float = 120,
) -> ReplayResult:
    pipeline = CameraPipeline(
        camera, schedule, detector_factories, max_event_seconds, classifiers=classifiers
    )
    if not pipeline.active:
        raise SystemExit("nothing to run: no enabled feature has its models available")
    width, height = size or (detail_size if pipeline.needs_detail else analysis_size)
    reader = FrameReader(ffmpeg, str(video), fps, width, height)
    t0 = start.timestamp()
    opened: dict = {}
    events: list[dict] = []

    def collect(outputs: list[RuleOutput]) -> None:
        for output in outputs:
            if isinstance(output, EventOpened):
                opened[output.event_id] = output
                continue
            assert isinstance(output, EventClosed)  # noqa: S101
            first = opened.pop(output.event_id, None)
            attributes = dict(output.rule.attributes)
            if first is not None and first.continues:
                attributes["continues"] = str(first.continues)
            events.append(
                {
                    "event_id": str(output.event_id),
                    "camera_id": str(camera.id),
                    "feature": output.rule.feature.value,
                    "event_type": output.rule.event_type.value,
                    "zone_id": str(first.zone_id) if first and first.zone_id else None,
                    "track_id": (
                        str(first.track_id) if first and first.track_id is not None else None
                    ),
                    "started_at": _iso(output.started_at),
                    "ended_at": _iso(output.ended_at),
                    "confidence": round(output.confidence, 3),
                    "attributes": attributes,
                }
            )

    frames = 0
    for _, frame in reader.frames():
        collect(pipeline.process(frame, t0 + frames / fps).outputs)
        frames += 1
    if frames == 0:
        error = reader.stderr_tail[-1] if reader.stderr_tail else "no frames"
        raise SystemExit(f"could not read {video}: {error}")
    collect(pipeline.engine.close_all())
    return ReplayResult(events, start, start + timedelta(seconds=frames / fps), frames)


def _aware(value: str) -> datetime:
    # Python 3.10's fromisoformat doesn't read a trailing "Z".
    when = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if when.tzinfo is None:
        raise argparse.ArgumentTypeError("--start needs a UTC offset, e.g. +03:00")
    return when.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cctv_edge.replay", description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--camera", type=Path, required=True, help="CameraConfig JSON")
    parser.add_argument("--start", type=_aware, required=True, help="time of the first frame")
    parser.add_argument("--timezone", default="Asia/Kuwait")
    parser.add_argument("--hours", type=Path, help="list of WorkingHours JSON (default: open)")
    parser.add_argument("--fps", type=float, default=5)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--models-dir", type=Path)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--out", type=Path, required=True, help="events JSONL (appended)")
    parser.add_argument("--coverage", type=Path, help="coverage CSV (appended)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = EdgeSettings()
    camera = CameraConfig.model_validate_json(args.camera.read_text(encoding="utf-8"))
    hours = (
        TypeAdapter(list[WorkingHours]).validate_json(args.hours.read_text(encoding="utf-8"))
        if args.hours
        else []
    )
    registry = ModelRegistry(args.models_dir or settings.models_path, settings.model_providers)
    result = replay(
        args.video,
        camera,
        args.start,
        BranchSchedule(hours, args.timezone),
        {**BUILTIN_DETECTORS, **registry.detector_factories()},
        registry.classifier_factories(),
        ffmpeg=args.ffmpeg or settings.ffmpeg,
        fps=args.fps,
        size=(args.width, args.height) if args.width and args.height else None,
        detail_size=(settings.detail_width, settings.detail_height),
        analysis_size=(settings.analysis_width, settings.analysis_height),
        max_event_seconds=settings.max_event_seconds,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as fh:
        for event in result.events:
            fh.write(json.dumps(event) + "\n")
    if args.coverage:
        new = not args.coverage.exists() or args.coverage.stat().st_size == 0
        with args.coverage.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(["camera_id", "started_at", "ended_at"])
            writer.writerow(
                [str(camera.id), result.started_at.isoformat(), result.ended_at.isoformat()]
            )
    log.info(
        "%s: %d frames, %d events -> %s",
        args.video.name,
        result.frames,
        len(result.events),
        args.out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
