"""Heatmaps from the edge (later phase): person-seconds per cell of a grid over each
camera's picture, per hour. Staff are already left out on the box."""

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.db.models import Camera, HeatmapBucket
from cctv_contracts.v1 import BatchResult, HeatmapBucketIn, RejectedItem

_UNIQUE = "uq_heatmap_buckets_camera_id_bucket_start"


def apply_heatmaps(
    db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID, buckets: Sequence[HeatmapBucketIn]
) -> BatchResult:
    """Store buckets of the branch's cameras; a bucket sent again replaces the earlier one."""
    cameras = set(db.scalars(select(Camera.id).where(Camera.branch_id == branch_id)))
    rejected: list[RejectedItem] = []
    rows: dict[tuple, dict] = {}  # by unique key: one row may appear only once per INSERT
    for index, bucket in enumerate(buckets):
        if bucket.camera_id not in cameras:
            rejected.append(RejectedItem(index=index, error="unknown camera"))
            continue
        rows[(bucket.camera_id, bucket.bucket_start)] = {
            "tenant_id": tenant_id,
            "camera_id": bucket.camera_id,
            "bucket_start": bucket.bucket_start,
            "bucket_seconds": bucket.bucket_seconds,
            "columns": bucket.columns,
            "rows": bucket.rows,
            "cells": [list(cell) for cell in bucket.cells],
        }
    if rows:
        statement = insert(HeatmapBucket).values(list(rows.values()))
        db.execute(
            statement.on_conflict_do_update(
                constraint=_UNIQUE,
                set_={
                    name: statement.excluded[name]
                    for name in ("bucket_seconds", "columns", "rows", "cells")
                },
            )
        )
    return BatchResult(accepted=len(buckets) - len(rejected), rejected=rejected)


# The picture in nine areas (3 x 3), as in the dashboard's Heatmap page.
AREAS = [
    f"{row}.{column}"
    for row in ("top", "middle", "bottom")
    for column in ("left", "centre", "right")
]


@dataclass
class Grid:
    columns: int
    rows: int
    seconds: list[int]  # row by row
    hours: float  # time covered by the buckets


def combine(buckets: Sequence[HeatmapBucket]) -> Grid | None:
    """The buckets added up. If the grid size changed in the period (a newer box), only the
    buckets with the most common size are used."""
    if not buckets:
        return None
    (columns, rows), _ = Counter((b.columns, b.rows) for b in buckets).most_common(1)[0]
    seconds = [0] * (columns * rows)
    covered = 0
    for bucket in buckets:
        if (bucket.columns, bucket.rows) != (columns, rows):
            continue
        covered += bucket.bucket_seconds
        for index, value in bucket.cells:
            if 0 <= index < len(seconds):
                seconds[index] += value
    return Grid(columns, rows, seconds, covered / 3600)


def busiest_areas(grid: Grid) -> list[tuple[str, int]]:
    """(area, person-seconds) of the nine areas with time in them, busiest first."""
    totals = [0] * 9
    for index, seconds in enumerate(grid.seconds):
        row = min(2, int((index // grid.columns + 0.5) / grid.rows * 3))
        column = min(2, int((index % grid.columns + 0.5) / grid.columns * 3))
        totals[row * 3 + column] += seconds
    found = [(AREAS[i], s) for i, s in enumerate(totals) if s > 0]
    return sorted(found, key=lambda a: -a[1])
