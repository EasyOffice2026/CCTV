"""Split footage into train / val / test by camera-day.

    python -m cctv_ml.split --inventory footage.csv --out manifest.csv

Input: one row per footage file with `path, camera_id, started_at, ended_at` (ISO-8601
with offset) and optionally `site`. Extra columns are kept.

All footage of one camera on one local calendar day (the day the file starts, in
`--timezone`) forms a group, and a group goes to exactly one split. Frames from the same
camera on the same day look almost alike, so splitting inside a day would leak test
footage into training and flatter the scores.

A group's split comes from a hash of (seed, camera, day), so it never depends on what
other footage exists: adding new days later doesn't move old ones. If `--out` already
exists, its assignments are kept as they are (pass `--fresh` to ignore it). Together this
means a camera-day that was once in **test** stays in test, and the test split is never
used for training.

With `--stratify-site`, new groups are instead given to the split furthest below its
target share of hours *within their site*, in hash order. That balances small datasets
better, and the kept assignments from `--out` still make it stable.
"""

import argparse
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cctv_ml.io import InputError, parse_time, read_csv, safe_console, write_csv

SPLITS = ("train", "val", "test")
REQUIRED = ("path", "camera_id", "started_at", "ended_at")

GroupKey = tuple[str, date]


@dataclass(frozen=True)
class Footage:
    line: int
    row: dict[str, str]
    camera_id: str
    started_at: datetime
    ended_at: datetime
    site: str

    @property
    def hours(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 3600


def load_inventory(path: Path) -> tuple[list[str], list[Footage]]:
    footage: list[Footage] = []
    columns: list[str] = []
    for line, row in read_csv(path, REQUIRED):
        if not columns:
            columns = list(row)
        where = f"{path}:{line}"
        started = parse_time(row["started_at"], f"{where} started_at")
        ended = parse_time(row["ended_at"], f"{where} ended_at")
        if ended <= started:
            raise InputError(f"{where}: ended_at is not after started_at")
        if not row["camera_id"]:
            raise InputError(f"{where}: camera_id is empty")
        footage.append(Footage(line, row, row["camera_id"], started, ended, row.get("site", "")))
    if not footage:
        raise InputError(f"{path}: no footage rows")
    return columns, footage


def group_key(item: Footage, tz: ZoneInfo) -> GroupKey:
    return item.camera_id, item.started_at.astimezone(tz).date()


def hash_unit(seed: str, key: GroupKey) -> float:
    """A stable number in [0, 1) for a group."""
    digest = hashlib.sha256(f"{seed}|{key[0]}|{key[1].isoformat()}".encode()).hexdigest()
    return int(digest[:15], 16) / 16**15


def by_hash(unit: float, ratios: dict[str, float]) -> str:
    edge = 0.0
    for split in SPLITS:
        edge += ratios[split]
        if unit < edge:
            return split
    return SPLITS[-1]


def parse_ratios(text: str) -> dict[str, float]:
    try:
        values = [float(v) for v in text.split(",")]
    except ValueError as exc:
        raise InputError(f"--ratios: expected three numbers, got {text!r}") from exc
    if len(values) != 3 or any(v < 0 for v in values) or sum(values) <= 0:
        raise InputError("--ratios: expected three non-negative numbers, e.g. 70,15,15")
    total = sum(values)
    return {split: v / total for split, v in zip(SPLITS, values, strict=True)}


def load_previous(path: Path) -> dict[GroupKey, str]:
    previous: dict[GroupKey, str] = {}
    for line, row in read_csv(path, ("camera_id", "day", "split")):
        if row["split"] not in SPLITS:
            raise InputError(f"{path}:{line}: unknown split {row['split']!r}")
        key = (row["camera_id"], date.fromisoformat(row["day"]))
        if previous.setdefault(key, row["split"]) != row["split"]:
            raise InputError(f"{path}:{line}: camera-day {key[0]} {key[1]} is in two splits")
    return previous


def assign(
    footage: list[Footage],
    ratios: dict[str, float],
    seed: str,
    tz: ZoneInfo,
    previous: dict[GroupKey, str] | None = None,
    stratify_site: bool = False,
) -> dict[GroupKey, str]:
    groups: dict[GroupKey, list[Footage]] = defaultdict(list)
    for item in footage:
        groups[group_key(item, tz)].append(item)

    previous = previous or {}
    assigned = {key: previous[key] for key in groups if key in previous}
    new = sorted((k for k in groups if k not in assigned), key=lambda k: hash_unit(seed, k))
    if not stratify_site:
        for key in new:
            assigned[key] = by_hash(hash_unit(seed, key), ratios)
        return assigned

    def site_of(key: GroupKey) -> str:
        sites = {item.site for item in groups[key]}
        if len(sites) > 1:
            raise InputError(f"camera {key[0]} has several sites on {key[1]}: {sorted(sites)}")
        return sites.pop()

    hours: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(SPLITS, 0.0))
    for key, split in assigned.items():
        hours[site_of(key)][split] += sum(item.hours for item in groups[key])
    for key in new:
        site = site_of(key)
        group_hours = sum(item.hours for item in groups[key])
        total = sum(hours[site].values()) + group_hours
        # The split furthest below its target share; ties go to test, then val, so small
        # sites still get test footage.
        split = max(reversed(SPLITS), key=lambda s: ratios[s] * total - hours[site][s])
        assigned[key] = split
        hours[site][split] += group_hours
    return assigned


def summary(footage: list[Footage], assigned: dict[GroupKey, str], tz: ZoneInfo) -> str:
    hours: dict[str, dict[str, float]] = defaultdict(lambda: dict.fromkeys(SPLITS, 0.0))
    days: dict[str, dict[str, set[date]]] = defaultdict(lambda: {s: set() for s in SPLITS})
    for item in footage:
        key = group_key(item, tz)
        split = assigned[key]
        hours[item.camera_id][split] += item.hours
        days[item.camera_id][split].add(key[1])

    lines = [
        "camera                                 train h    val h   test h   days (t/v/test)",
    ]
    totals = dict.fromkeys(SPLITS, 0.0)
    warnings = []
    for camera in sorted(hours):
        h = hours[camera]
        d = days[camera]
        lines.append(
            f"{camera[:38]:<38} {h['train']:>8.1f} {h['val']:>8.1f} {h['test']:>8.1f}"
            f"   {len(d['train'])}/{len(d['val'])}/{len(d['test'])}"
        )
        for split in SPLITS:
            totals[split] += h[split]
        if sum(len(v) for v in d.values()) >= 3 and not d["test"]:
            warnings.append(f"warning: camera {camera} has no test footage")
    grand = sum(totals.values()) or 1.0
    lines.append(
        f"{'total':<38} {totals['train']:>8.1f} {totals['val']:>8.1f} {totals['test']:>8.1f}"
        f"   ({totals['train'] / grand:.0%} / {totals['val'] / grand:.0%}"
        f" / {totals['test'] / grand:.0%})"
    )
    return "\n".join(lines + warnings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cctv_ml.split", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--inventory", type=Path, required=True, help="footage CSV")
    parser.add_argument("--out", type=Path, required=True, help="manifest CSV to write")
    parser.add_argument("--ratios", default="70,15,15", help="train,val,test (default 70,15,15)")
    parser.add_argument("--seed", default="cctv-v1", help="change only to start a new split")
    parser.add_argument("--timezone", default="Asia/Kuwait", help="for the calendar day")
    parser.add_argument("--stratify-site", action="store_true", help="balance hours per site")
    parser.add_argument("--fresh", action="store_true", help="ignore an existing --out file")
    args = parser.parse_args(argv)
    safe_console()

    try:
        tz = ZoneInfo(args.timezone)
        columns, footage = load_inventory(args.inventory)
        previous = load_previous(args.out) if args.out.exists() and not args.fresh else None
        assigned = assign(
            footage, parse_ratios(args.ratios), args.seed, tz, previous, args.stratify_site
        )
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    extra = [c for c in ("day", "split") if c not in columns]
    rows = []
    for item in footage:
        key = group_key(item, tz)
        rows.append({**item.row, "day": key[1].isoformat(), "split": assigned[key]})
    write_csv(args.out, [*columns, *extra], rows)
    if previous:
        print(f"kept {sum(1 for k in assigned if k in previous)} camera-days from {args.out}")
    print(summary(footage, assigned, tz))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
