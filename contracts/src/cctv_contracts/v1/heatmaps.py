"""Where people stood, per camera, aggregated on the edge into time buckets (heatmaps).

The camera picture is divided into `columns` x `rows` cells (in normalised coordinates,
like zones). Each cell holds the person-seconds spent standing there (a track's foot
point) in the bucket. Staff, i.e. people seen in a staff area, kitchen or counter zone of
the camera, are left out, as in customer counts. Only cells with time in them are sent,
as (cell index, seconds) with index = row * columns + column.

The box sends each bucket once it is complete; resending the same bucket replaces it.
No pictures and no tracks leave the box: only these totals.
"""

from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from cctv_contracts.v1.common import ContractModel, UtcDatetime

HEATMAP_COLUMNS = 32
HEATMAP_ROWS = 18  # 16:9 cells on a 16:9 picture
MAX_HEATMAPS_PER_BATCH = 100

CellSeconds = tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=1)]]


class HeatmapBucketIn(ContractModel):
    camera_id: UUID
    bucket_start: UtcDatetime
    bucket_seconds: int = Field(default=3600, ge=300, le=3600)
    columns: int = Field(default=HEATMAP_COLUMNS, ge=1, le=64)
    rows: int = Field(default=HEATMAP_ROWS, ge=1, le=64)
    cells: list[CellSeconds] = Field(default_factory=list, max_length=4096)

    @model_validator(mode="after")
    def _check(self) -> "HeatmapBucketIn":
        if int(self.bucket_start.timestamp()) % self.bucket_seconds or (
            self.bucket_start.microsecond
        ):
            raise ValueError("bucket_start must be a multiple of bucket_seconds (UTC)")
        indexes = [index for index, _ in self.cells]
        if len(set(indexes)) != len(indexes):
            raise ValueError("a cell appears twice")
        if any(index >= self.columns * self.rows for index in indexes):
            raise ValueError("cell index outside the grid")
        return self


class HeatmapBatch(ContractModel):
    buckets: list[HeatmapBucketIn] = Field(min_length=1, max_length=MAX_HEATMAPS_PER_BATCH)
