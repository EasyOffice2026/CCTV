"""Event-level evaluation of the edge rules on labelled, held-out footage (plan 5.5).

    python -m cctv_ml.evaluate --predictions events.jsonl --ground-truth gt.csv \
        --coverage coverage.csv --out report.md --json report.json

- predictions: JSON Lines written by the edge replay tool, one event per line with
  `event_id, camera_id, feature, event_type, zone_id, track_id, started_at, ended_at,
  confidence, attributes` (times ISO-8601 with offset; `ended_at` may be null).
- ground truth: CSV `camera_id, event_type, started_at, ended_at[, note]`.
- coverage: CSV `camera_id, started_at, ended_at`, the periods that were both labelled
  and replayed. Events that start outside them are ignored.

Matching, per camera and event type: a prediction and a labelled event match when their
intervals overlap, allowing `--tolerance` seconds of gap. Pairs are taken greedily by
overlap, each labelled event and each prediction at most once. A prediction left over
that still overlaps a matched labelled event is a **duplicate** (e.g. a long event split
into parts): it is counted separately, not as a false positive. Every other prediction
left over is a false positive (a false alarm), and every labelled event left over is a
miss.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cctv_contracts.enums import EVENT_FEATURE, EventType
from cctv_ml.io import InputError, Span, fmt_ratio, merge_spans, parse_time, read_csv, safe_console

DEFAULT_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
MAX_LISTED_FAILURES = 200


@dataclass(frozen=True)
class Truth:
    camera_id: str
    event_type: EventType
    start: datetime
    end: datetime
    note: str = ""
    where: str = ""


@dataclass(frozen=True)
class Prediction:
    camera_id: str
    event_type: EventType
    start: datetime
    end: datetime
    confidence: float
    event_id: str = ""


def _event_type(value: object, where: str) -> EventType:
    try:
        return EventType(str(value))
    except ValueError as exc:
        raise InputError(f"{where}: unknown event_type {value!r}") from exc


def load_predictions(path: Path) -> list[Prediction]:
    predictions = []
    with path.open(encoding="utf-8") as handle:
        for number, text in enumerate(handle, 1):
            if not text.strip():
                continue
            where = f"{path}:{number}"
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InputError(f"{where}: not JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise InputError(f"{where}: expected a JSON object")
            for name in ("camera_id", "event_type", "started_at"):
                if not obj.get(name):
                    raise InputError(f"{where}: missing {name}")
            event_type = _event_type(obj["event_type"], where)
            feature = obj.get("feature")
            if feature and feature != EVENT_FEATURE[event_type].value:
                raise InputError(f"{where}: {event_type.value} is not part of feature {feature}")
            start = parse_time(obj["started_at"], f"{where} started_at")
            end = parse_time(obj["ended_at"], f"{where} ended_at") if obj.get("ended_at") else start
            if end < start:
                raise InputError(f"{where}: ended_at is before started_at")
            confidence = float(obj.get("confidence", 1.0))
            predictions.append(
                Prediction(
                    str(obj["camera_id"]),
                    event_type,
                    start,
                    end,
                    confidence,
                    str(obj.get("event_id") or f"line {number}"),
                )
            )
    return predictions


def load_truths(path: Path) -> list[Truth]:
    truths = []
    for line, row in read_csv(path, ("camera_id", "event_type", "started_at", "ended_at")):
        where = f"{path}:{line}"
        start = parse_time(row["started_at"], f"{where} started_at")
        end = parse_time(row["ended_at"], f"{where} ended_at")
        if end < start:
            raise InputError(f"{where}: ended_at is before started_at")
        truths.append(
            Truth(
                row["camera_id"],
                _event_type(row["event_type"], where),
                start,
                end,
                row.get("note", ""),
                where,
            )
        )
    return truths


def load_coverage(path: Path) -> dict[str, list[Span]]:
    spans: dict[str, list[Span]] = defaultdict(list)
    for line, row in read_csv(path, ("camera_id", "started_at", "ended_at")):
        where = f"{path}:{line}"
        start = parse_time(row["started_at"], f"{where} started_at")
        end = parse_time(row["ended_at"], f"{where} ended_at")
        if end <= start:
            raise InputError(f"{where}: ended_at is not after started_at")
        spans[row["camera_id"]].append((start, end))
    if not spans:
        raise InputError(f"{path}: no coverage rows")
    return {camera: merge_spans(s) for camera, s in spans.items()}


def covered(coverage: dict[str, list[Span]], camera_id: str, at: datetime) -> bool:
    return any(start <= at < end for start, end in coverage.get(camera_id, ()))


@dataclass
class Matching:
    matched: list[tuple[Truth, Prediction]] = field(default_factory=list)
    false_positives: list[Prediction] = field(default_factory=list)
    misses: list[Truth] = field(default_factory=list)
    duplicates: list[tuple[Prediction, Truth]] = field(default_factory=list)


def _overlap(t: Truth, p: Prediction) -> float:
    return (min(t.end, p.end) - max(t.start, p.start)).total_seconds()


def match(truths: Sequence[Truth], predictions: Sequence[Prediction], tolerance: float) -> Matching:
    result = Matching()
    groups: dict[tuple[str, EventType], tuple[list[Truth], list[Prediction]]] = defaultdict(
        lambda: ([], [])
    )
    for t in truths:
        groups[(t.camera_id, t.event_type)][0].append(t)
    for p in predictions:
        groups[(p.camera_id, p.event_type)][1].append(p)

    for key in sorted(groups, key=lambda k: (k[0], k[1].value)):
        group_truths, group_predictions = groups[key]
        pairs = sorted(
            (
                (
                    -_overlap(t, p),
                    abs((p.start - t.start).total_seconds()),
                    ti,
                    pi,
                )
                for ti, t in enumerate(group_truths)
                for pi, p in enumerate(group_predictions)
                if _overlap(t, p) >= -tolerance
            ),
        )
        truth_used: set[int] = set()
        prediction_used: set[int] = set()
        for _, _, ti, pi in pairs:
            if ti in truth_used or pi in prediction_used:
                continue
            truth_used.add(ti)
            prediction_used.add(pi)
            result.matched.append((group_truths[ti], group_predictions[pi]))
        for pi, p in enumerate(group_predictions):
            if pi in prediction_used:
                continue
            overlapping = [
                group_truths[ti] for ti in truth_used if _overlap(group_truths[ti], p) >= -tolerance
            ]
            if overlapping:
                best = max(overlapping, key=lambda t: _overlap(t, p))
                result.duplicates.append((p, best))
            else:
                result.false_positives.append(p)
        result.misses.extend(t for ti, t in enumerate(group_truths) if ti not in truth_used)
    return result


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    duplicates: int = 0
    delays: list[float] = field(default_factory=list)  # prediction start - labelled start (s)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def median_delay(self) -> float | None:
        return statistics.median(self.delays) if self.delays else None


def count_by(matching: Matching, key) -> dict[Any, Counts]:
    """Counts per `key(event_type)`, e.g. per event type or per feature."""
    counts: dict[Any, Counts] = defaultdict(Counts)
    for t, p in matching.matched:
        c = counts[key(t.event_type)]
        c.tp += 1
        c.delays.append((p.start - t.start).total_seconds())
    for p in matching.false_positives:
        counts[key(p.event_type)].fp += 1
    for t in matching.misses:
        counts[key(t.event_type)].fn += 1
    for p, _ in matching.duplicates:
        counts[key(p.event_type)].duplicates += 1
    return counts


@dataclass(frozen=True)
class CoverageStats:
    cameras: int
    camera_hours: float  # sum over cameras of their covered hours
    wall_hours: float  # hours covered by at least one camera

    @property
    def camera_days(self) -> float:
        return self.camera_hours / 24

    @property
    def days(self) -> float:
        return self.wall_hours / 24


def coverage_stats(coverage: dict[str, list[Span]]) -> CoverageStats:
    hour = timedelta(hours=1)
    camera_hours = sum(
        (end - start) / hour for spans in coverage.values() for start, end in merge_spans(spans)
    )
    wall = merge_spans(span for spans in coverage.values() for span in spans)
    return CoverageStats(len(coverage), camera_hours, sum((e - s) / hour for s, e in wall))


def per_day(value: int, days: float) -> float | None:
    return value / days if days > 0 else None


@dataclass
class Evaluation:
    truths: list[Truth]
    predictions: list[Prediction]
    ignored_truths: int
    ignored_predictions: int
    stats: CoverageStats
    matching: Matching
    tolerance: float
    target: float
    max_false_alarms: float
    thresholds: Sequence[float]

    def by_type(self) -> dict[EventType, Counts]:
        return count_by(self.matching, lambda e: e)

    def by_feature(self) -> dict[str, Counts]:
        return count_by(self.matching, lambda e: EVENT_FEATURE[e].value)

    def feature_status(self, counts: Counts) -> str:
        if counts.precision is None or counts.recall is None:
            return "not enough data"
        ok = counts.precision >= self.target and counts.recall >= self.target
        return "pass" if ok else "fail"

    def sweep(self) -> dict[EventType, list[tuple[float, Counts]]]:
        types = sorted(
            {t.event_type for t in self.truths} | {p.event_type for p in self.predictions},
            key=lambda e: e.value,
        )
        result: dict[EventType, list[tuple[float, Counts]]] = {e: [] for e in types}
        for threshold in self.thresholds:
            kept = [p for p in self.predictions if p.confidence >= threshold]
            counts = count_by(match(self.truths, kept, self.tolerance), lambda e: e)
            for event_type in types:
                result[event_type].append((threshold, counts.get(event_type, Counts())))
        return result


def evaluate(
    truths: Iterable[Truth],
    predictions: Iterable[Prediction],
    coverage: dict[str, list[Span]],
    tolerance: float = 2.0,
    target: float = 0.9,
    max_false_alarms: float = 5.0,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
) -> Evaluation:
    all_truths, all_predictions = list(truths), list(predictions)
    kept_truths = [t for t in all_truths if covered(coverage, t.camera_id, t.start)]
    kept_predictions = [p for p in all_predictions if covered(coverage, p.camera_id, p.start)]
    return Evaluation(
        kept_truths,
        kept_predictions,
        len(all_truths) - len(kept_truths),
        len(all_predictions) - len(kept_predictions),
        coverage_stats(coverage),
        match(kept_truths, kept_predictions, tolerance),
        tolerance,
        target,
        max_false_alarms,
        thresholds,
    )


# --- report ------------------------------------------------------------------------------


def _counts_json(c: Counts, stats: CoverageStats) -> dict[str, Any]:
    return {
        "tp": c.tp,
        "fp": c.fp,
        "fn": c.fn,
        "duplicates": c.duplicates,
        "precision": c.precision,
        "recall": c.recall,
        "f1": c.f1,
        "false_alarms_per_day": per_day(c.fp, stats.days),
        "false_alarms_per_camera_day": per_day(c.fp, stats.camera_days),
        "median_start_delay_s": c.median_delay,
    }


def to_json(ev: Evaluation) -> dict[str, Any]:
    stats = ev.stats
    total = count_by(ev.matching, lambda _e: "all")["all"]
    return {
        "coverage": {
            "cameras": stats.cameras,
            "camera_hours": stats.camera_hours,
            "wall_hours": stats.wall_hours,
        },
        "labelled_events": len(ev.truths),
        "predicted_events": len(ev.predictions),
        "ignored_outside_coverage": {
            "labelled": ev.ignored_truths,
            "predicted": ev.ignored_predictions,
        },
        "tolerance_s": ev.tolerance,
        "target": ev.target,
        "overall": {
            **_counts_json(total, stats),
            "false_alarm_target_per_day": ev.max_false_alarms,
        },
        "features": {
            feature: {**_counts_json(c, stats), "status": ev.feature_status(c)}
            for feature, c in sorted(ev.by_feature().items())
        },
        "event_types": {
            e.value: _counts_json(c, stats)
            for e, c in sorted(ev.by_type().items(), key=lambda i: i[0].value)
        },
        "threshold_sweep": {
            e.value: [{"threshold": t, **_counts_json(c, stats)} for t, c in rows]
            for e, rows in ev.sweep().items()
        },
        "false_positives": [
            {
                "camera_id": p.camera_id,
                "event_type": p.event_type.value,
                "started_at": p.start.isoformat(),
                "ended_at": p.end.isoformat(),
                "confidence": p.confidence,
                "event_id": p.event_id,
            }
            for p in ev.matching.false_positives
        ],
        "misses": [
            {
                "camera_id": t.camera_id,
                "event_type": t.event_type.value,
                "started_at": t.start.isoformat(),
                "ended_at": t.end.isoformat(),
                "note": t.note,
            }
            for t in ev.matching.misses
        ],
    }


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _metrics_row(name: str, c: Counts, stats: CoverageStats) -> str:
    return (
        f"| {name} | {c.tp} | {c.fp} | {c.fn} | {c.duplicates} | {fmt_ratio(c.precision)}"
        f" | {fmt_ratio(c.recall)} | {fmt_ratio(c.f1)} | {_num(per_day(c.fp, stats.days), 2)}"
        f" | {_num(per_day(c.fp, stats.camera_days), 2)} | {_num(c.median_delay)} |"
    )


_METRICS_HEADER = (
    "| | TP | FP | FN | Dup. | Precision | Recall | F1 | FA/day | FA/camera-day"
    " | Median start delay (s) |\n|---|---|---|---|---|---|---|---|---|---|---|"
)


def to_markdown(ev: Evaluation, tz: ZoneInfo) -> str:
    stats = ev.stats
    total = count_by(ev.matching, lambda _e: "all")["all"]
    fa_day = per_day(total.fp, stats.days)

    def when(at: datetime) -> str:
        return at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")

    out = [
        "# Event evaluation report",
        "",
        f"- Footage covered: **{stats.wall_hours:.1f} h** ({stats.days:.2f} days) across"
        f" {stats.cameras} camera(s), {stats.camera_hours:.1f} camera-hours"
        f" ({stats.camera_days:.2f} camera-days)",
        f"- Labelled events: {len(ev.truths)}; predicted events: {len(ev.predictions)}"
        f" (ignored outside coverage: {ev.ignored_truths} labelled,"
        f" {ev.ignored_predictions} predicted)",
        f"- Matching tolerance: +/-{ev.tolerance:g} s; times below are in {tz.key}",
        "",
        f"## Target check (precision and recall >= {ev.target:.0%} per feature)",
        "",
        "| Feature | Precision | Recall | FA/day | Status |",
        "|---|---|---|---|---|",
    ]
    for feature, c in sorted(ev.by_feature().items()):
        out.append(
            f"| {feature} | {fmt_ratio(c.precision)} | {fmt_ratio(c.recall)}"
            f" | {_num(per_day(c.fp, stats.days), 2)} | **{ev.feature_status(c)}** |"
        )
    if fa_day is None:
        fa_status = "not enough data"
    else:
        fa_status = "pass" if fa_day < ev.max_false_alarms else "fail"
    out += [
        "",
        f"False alarms, all features: **{_num(fa_day, 2)} per day** (target"
        f" < {ev.max_false_alarms:g} per branch per day): **{fa_status}**. This compares"
        " with the pilot target only when the cameras evaluated are one branch's.",
        "",
        "## Per feature",
        "",
        _METRICS_HEADER,
    ]
    out += [_metrics_row(f, c, stats) for f, c in sorted(ev.by_feature().items())]
    out += ["", "## Per event type", "", _METRICS_HEADER]
    out += [
        _metrics_row(e.value, c, stats)
        for e, c in sorted(ev.by_type().items(), key=lambda i: i[0].value)
    ]
    out += [
        "",
        "## Confidence threshold sweep",
        "",
        "Only predictions with confidence >= the threshold are kept. Use it to choose the"
        " threshold for each rule; check it again on the next held-out footage.",
    ]
    for event_type, rows in ev.sweep().items():
        out += [
            "",
            f"### {event_type.value}",
            "",
            "| Threshold | TP | FP | FN | Precision | Recall | FA/day |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {t:.2f} | {c.tp} | {c.fp} | {c.fn} | {fmt_ratio(c.precision)}"
            f" | {fmt_ratio(c.recall)} | {_num(per_day(c.fp, stats.days), 2)} |"
            for t, c in rows
        ]

    fps = sorted(ev.matching.false_positives, key=lambda p: (p.event_type.value, p.start))
    misses = sorted(ev.matching.misses, key=lambda t: (t.event_type.value, t.start))
    out += ["", f"## False alarms ({len(fps)})", ""]
    if fps:
        out += [
            "| Event type | Camera | Start | End | Confidence | Event id |",
            "|---|---|---|---|---|---|",
        ]
        out += [
            f"| {p.event_type.value} | {p.camera_id} | {when(p.start)} | {when(p.end)}"
            f" | {p.confidence:.2f} | {p.event_id} |"
            for p in fps[:MAX_LISTED_FAILURES]
        ]
    out += ["", f"## Missed events ({len(misses)})", ""]
    if misses:
        out += ["| Event type | Camera | Start | End | Note |", "|---|---|---|---|---|"]
        out += [
            f"| {t.event_type.value} | {t.camera_id} | {when(t.start)} | {when(t.end)} | {t.note} |"
            for t in misses[:MAX_LISTED_FAILURES]
        ]
    if len(fps) > MAX_LISTED_FAILURES or len(misses) > MAX_LISTED_FAILURES:
        out += ["", f"Lists are cut at {MAX_LISTED_FAILURES}; the JSON report has all of them."]
    out += [
        "",
        "## How the numbers are computed",
        "",
        "- A prediction matches a labelled event of the same camera and type when their"
        f" intervals overlap, allowing a gap of up to {ev.tolerance:g} s. Pairs are taken by"
        " largest overlap; each event is matched at most once.",
        "- **Dup.** (duplicates) are extra predictions overlapping an already matched"
        " event, e.g. a long event split into parts. They are not counted as false alarms;"
        " many duplicates still mean repeated alerts, so check the cooldown.",
        "- Precision = TP / (TP + FP); recall = TP / (TP + FN).",
        "- FA/day = FP / (hours covered by at least one camera / 24). For one branch's"
        " cameras over the same period this is false alarms per branch per day.",
        "- FA/camera-day = FP / (sum of each camera's covered hours / 24).",
        "- Start delay = predicted start - labelled start; rules wait their minimum duration,"
        " so a few seconds is expected.",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cctv_ml.evaluate", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--predictions", type=Path, required=True, help="replay JSONL")
    parser.add_argument("--ground-truth", type=Path, required=True, help="labelled events CSV")
    parser.add_argument("--coverage", type=Path, required=True, help="covered periods CSV")
    parser.add_argument("--tolerance", type=float, default=2.0, help="seconds (default 2)")
    parser.add_argument("--target", type=float, default=0.9, help="precision/recall target")
    parser.add_argument("--max-false-alarms", type=float, default=5.0, help="per day")
    parser.add_argument(
        "--thresholds",
        default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
        help="confidence thresholds for the sweep",
    )
    parser.add_argument("--timezone", default="Asia/Kuwait", help="for times in the report")
    parser.add_argument("--out", type=Path, help="markdown report (default: print)")
    parser.add_argument("--json", type=Path, help="also write the full results as JSON")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if a feature with data misses the target"
    )
    args = parser.parse_args(argv)
    safe_console()

    try:
        thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
        ev = evaluate(
            load_truths(args.ground_truth),
            load_predictions(args.predictions),
            load_coverage(args.coverage),
            args.tolerance,
            args.target,
            args.max_false_alarms,
            thresholds,
        )
    except (InputError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = to_markdown(ev, ZoneInfo(args.timezone))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
    else:
        print(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(to_json(ev), indent=2), encoding="utf-8", newline="\n")
        print(f"wrote {args.json}")
    if args.check and any(ev.feature_status(c) == "fail" for c in ev.by_feature().values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
