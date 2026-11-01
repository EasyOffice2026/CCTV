"""Heatmap buckets: whole buckets, cells inside the grid, each cell once."""

import uuid

import pytest
from pydantic import ValidationError

from cctv_contracts.v1 import HeatmapBucketIn

HOUR = "2026-10-31T09:00:00+00:00"


def bucket(**values) -> HeatmapBucketIn:
    return HeatmapBucketIn(camera_id=uuid.uuid4(), **{"bucket_start": HOUR, **values})


def test_a_valid_bucket():
    b = bucket(cells=[(0, 5), (575, 3600)])
    assert (b.columns, b.rows, b.bucket_seconds) == (32, 18, 3600)


@pytest.mark.parametrize(
    "values",
    [
        {"cells": [(576, 1)]},  # outside the 32 x 18 grid
        {"cells": [(3, 1), (3, 2)]},  # the same cell twice
        {"cells": [(3, 0)]},  # empty cells are left out
        {"bucket_start": "2026-10-31T09:30:00+00:00"},  # not a whole hour
    ],
)
def test_invalid_buckets(values):
    with pytest.raises(ValidationError):
        bucket(**values)
