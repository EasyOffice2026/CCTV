"""CSV and time helpers shared by the tools."""

import csv
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path


def safe_console() -> None:
    """Don't crash printing non-ASCII names (Arabic notes, camera names) on a Windows
    console with a legacy code page; unprintable characters become '?'."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


class InputError(ValueError):
    """A problem with an input file; the message says where."""


def parse_time(value: str, where: str = "time") -> datetime:
    """ISO-8601 time with a UTC offset, returned in UTC. Times without an offset are
    rejected: NVR and laptop clocks disagree too often to guess (plan §0)."""
    text = (value or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InputError(f"{where}: not an ISO-8601 time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise InputError(f"{where}: {value!r} has no UTC offset")
    return parsed.astimezone(timezone.utc)


def read_csv(path: Path, required: Sequence[str]) -> Iterator[tuple[int, dict[str, str]]]:
    """Rows of a CSV file with a header, as (line number, row). Values are stripped."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise InputError(f"{path}: missing column(s) {', '.join(missing)}")
        for row in reader:
            if not any((v or "").strip() for v in row.values()):
                continue
            yield reader.line_num, {k: (v or "").strip() for k, v in row.items() if k}


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


Span = tuple[datetime, datetime]


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """Sorted, non-overlapping union of time spans."""
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def total_hours(spans: Iterable[Span]) -> float:
    return sum(((end - start) for start, end in merge_spans(spans)), timedelta()) / timedelta(
        hours=1
    )


def fmt_ratio(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"
