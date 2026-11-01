"""Confidence thresholds from shadow-mode reviews (plan 9.5).

    python -m cctv_ml.review_sweep --manifest manifest.csv --out sweep.md [--json sweep.json]
        [--target 0.9] [--days 14]

`manifest.csv` comes from the cloud (`python -m cctv_cloud.cli export-reviewed`): one row
per event that a person marked `valid` or `false_alarm` on the dashboard, with the
model's `confidence`. Other review statuses are ignored.

For each event type, every threshold the data allows is tried: events below it would not
have been raised. The report shows how many confirmed events would still be raised
("kept"), how many false alarms remain, the precision and the false alarms per day, and
the lowest threshold that reaches `--target` precision.

This only sees what the system raised: events it missed are not in the reviews, so a
higher threshold can look free here and still lose real events that the reviews never
showed. Check a new threshold on the labelled test set (`cctv_ml.evaluate`) before using
it, and treat it as a starting point for the shadow period, not a result.
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from cctv_ml.io import InputError, parse_time, read_csv, safe_console

REQUIRED = ("event_type", "confidence", "review_status", "started_at")


@dataclass(frozen=True)
class Reviewed:
    event_type: str
    confidence: float
    valid: bool
    day: str


@dataclass
class Step:
    threshold: float
    kept_valid: int
    kept_false: int
    precision: float | None
    kept_share: float | None  # of confirmed events, how many are still raised
    false_per_day: float


@dataclass
class TypeSweep:
    event_type: str
    valid: int
    false_alarm: int
    steps: list[Step] = field(default_factory=list)
    suggested: Step | None = None  # lowest threshold reaching the target


def load(path: Path) -> list[Reviewed]:
    rows = []
    for line, row in read_csv(path, REQUIRED):
        status = row["review_status"]
        if status not in ("valid", "false_alarm"):
            continue
        try:
            confidence = float(row["confidence"])
        except ValueError as exc:
            raise InputError(f"{path}:{line}: confidence {row['confidence']!r}") from exc
        started = parse_time(row["started_at"], f"{path}:{line}: started_at")
        rows.append(
            Reviewed(row["event_type"], confidence, status == "valid", started.date().isoformat())
        )
    return rows


def sweep(rows: list[Reviewed], days: float, target: float) -> list[TypeSweep]:
    by_type: dict[str, list[Reviewed]] = defaultdict(list)
    for r in rows:
        by_type[r.event_type].append(r)
    result = []
    for event_type, items in sorted(by_type.items()):
        valid = sum(r.valid for r in items)
        report = TypeSweep(event_type, valid, len(items) - valid)
        for threshold in sorted({0.0, *(r.confidence for r in items)}):
            kept = [r for r in items if r.confidence >= threshold]
            kept_valid = sum(r.valid for r in kept)
            kept_false = len(kept) - kept_valid
            step = Step(
                threshold=round(threshold, 3),
                kept_valid=kept_valid,
                kept_false=kept_false,
                precision=round(kept_valid / len(kept), 3) if kept else None,
                kept_share=round(kept_valid / valid, 3) if valid else None,
                false_per_day=round(kept_false / days, 2) if days else 0.0,
            )
            report.steps.append(step)
            if report.suggested is None and step.precision is not None and step.precision >= target:
                report.suggested = step
        result.append(report)
    return result


def span_days(rows: list[Reviewed]) -> int:
    days = sorted({r.day for r in rows})
    if not days:
        return 0
    return (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.0f}%"


def to_markdown(sweeps: list[TypeSweep], days: float, target: float) -> str:
    lines = [
        "# Thresholds from shadow-mode reviews",
        "",
        f"Reviewed over {days:g} days; target precision {_pct(target)}. Only events the "
        "system raised are here: check any new threshold on the labelled test set.",
        "",
        "| Event type | Valid | False alarms | Precision now | Suggested threshold | "
        "Keeps | False alarms/day after |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in sweeps:
        now = s.steps[0]
        if s.suggested is None:
            suggestion = keeps = after = "not reached"
        else:
            suggestion = f"{s.suggested.threshold:g}"
            keeps = _pct(s.suggested.kept_share)
            after = f"{s.suggested.false_per_day:g}"
        lines.append(
            f"| {s.event_type} | {s.valid} | {s.false_alarm} | {_pct(now.precision)} | "
            f"{suggestion} | {keeps} | {after} |"
        )
    for s in sweeps:
        lines += [
            "",
            f"## {s.event_type}",
            "",
            "| Threshold | Valid kept | False alarms kept | Precision | Share of valid kept "
            "| False alarms/day |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {st.threshold:g} | {st.kept_valid} | {st.kept_false} | {_pct(st.precision)} | "
            f"{_pct(st.kept_share)} | {st.false_per_day:g} |"
            for st in s.steps
        ]
    return "\n".join(lines) + "\n"


def to_json(sweeps: list[TypeSweep], days: float, target: float) -> dict[str, Any]:
    def step(st: Step | None) -> dict[str, Any] | None:
        return None if st is None else st.__dict__

    return {
        "days": days,
        "target": target,
        "event_types": [
            {
                "event_type": s.event_type,
                "valid": s.valid,
                "false_alarm": s.false_alarm,
                "suggested": step(s.suggested),
                "steps": [step(st) for st in s.steps],
            }
            for s in sweeps
        ],
    }


def main(argv: list[str] | None = None) -> int:
    safe_console()
    parser = argparse.ArgumentParser(prog="python -m cctv_ml.review_sweep")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Markdown report")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--target", type=float, default=0.9, help="precision to reach")
    parser.add_argument("--days", type=float, help="length of the review period (default: span)")
    args = parser.parse_args(argv)
    try:
        rows = load(args.manifest)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    days = args.days or span_days(rows)
    sweeps = sweep(rows, days, args.target)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(to_markdown(sweeps, days, args.target), encoding="utf-8")
    if args.json:
        args.json.write_text(
            json.dumps(to_json(sweeps, days, args.target), indent=2), encoding="utf-8"
        )
    for s in sweeps:
        hint = f"threshold {s.suggested.threshold:g}" if s.suggested else "target not reached"
        print(f"{s.event_type}: {s.valid} valid, {s.false_alarm} false alarms -> {hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
