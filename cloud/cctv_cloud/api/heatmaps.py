"""Heatmaps (later phase): where people stood in a camera's view over a period."""

import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from cctv_cloud.api.deps import Context, context, not_found
from cctv_cloud.db.models import Camera, HeatmapBucket
from cctv_cloud.rbac import Permission
from cctv_cloud.services.heatmaps import combine

router = APIRouter(prefix="/v1", tags=["counts"])

Viewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW), scope="function")]

MAX_RANGE = timedelta(days=93)


class HeatmapOut(BaseModel):
    camera_id: uuid.UUID
    columns: int
    rows: int
    seconds: list[int] = Field(
        description="Person-seconds per cell, row by row from the top left (cell index = "
        "row * columns + column); staff left out"
    )
    hours: float = Field(description="Hours of data in the period (the box sends each hour)")


@router.get("/heatmaps", response_model=HeatmapOut | None)
def get_heatmap(
    ctx: Viewer,
    camera_id: uuid.UUID,
    start: Annotated[datetime, Query(alias="from")],
    end: Annotated[datetime, Query(alias="to")],
) -> HeatmapOut | None:
    """Where people (not staff) stood in the camera's view between `from` and `to`, added
    up hour by hour; null when the camera sent no heatmap for the period. Needs the Heatmap
    feature on for the camera."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "from and to need a UTC offset")
    if not timedelta(0) < end - start <= MAX_RANGE:
        raise HTTPException(422, f"choose a range of at most {MAX_RANGE.days} days")
    camera = ctx.db.get(Camera, camera_id)
    if camera is None or not ctx.principal.can_access_branch(camera.branch_id):
        raise not_found("camera not found")
    buckets = ctx.db.scalars(
        select(HeatmapBucket).where(
            HeatmapBucket.camera_id == camera_id,
            HeatmapBucket.bucket_start >= start,
            HeatmapBucket.bucket_start < end,
        )
    ).all()
    grid = combine(buckets)
    if grid is None:
        return None
    return HeatmapOut(
        camera_id=camera_id,
        columns=grid.columns,
        rows=grid.rows,
        seconds=grid.seconds,
        hours=grid.hours,
    )
