"""People counts from the edge (line in/out, zone occupancy) per time bucket."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.db.models import Camera, CountBucket, Zone
from cctv_contracts.v1 import BatchResult, CountBucketIn, RejectedItem

_UNIQUE = "uq_count_buckets_camera_id_zone_id_bucket_start"


def apply_counts(
    db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID, buckets: Sequence[CountBucketIn]
) -> BatchResult:
    """Store buckets of the branch's cameras; a bucket sent again replaces the earlier one."""
    cameras = set(db.scalars(select(Camera.id).where(Camera.branch_id == branch_id)))
    zones = dict(
        db.execute(
            select(Zone.id, Zone.camera_id)
            .join(Camera, Camera.id == Zone.camera_id)
            .where(Camera.branch_id == branch_id)
        ).all()
    )
    rejected: list[RejectedItem] = []
    rows: dict[tuple, dict] = {}  # by unique key: one row may appear only once per INSERT
    for index, bucket in enumerate(buckets):
        if bucket.camera_id not in cameras:
            rejected.append(RejectedItem(index=index, error="unknown camera"))
            continue
        if bucket.zone_id is not None and zones.get(bucket.zone_id) != bucket.camera_id:
            rejected.append(RejectedItem(index=index, error="unknown zone"))
            continue
        rows[(bucket.camera_id, bucket.zone_id, bucket.bucket_start)] = {
            "tenant_id": tenant_id,
            **bucket.model_dump(
                include={
                    "camera_id",
                    "zone_id",
                    "bucket_start",
                    "bucket_seconds",
                    "in_count",
                    "out_count",
                    "max_occupancy",
                    "occupied_seconds",
                }
            ),
        }
    if rows:
        statement = insert(CountBucket).values(list(rows.values()))
        db.execute(
            statement.on_conflict_do_update(
                constraint=_UNIQUE,
                set_={
                    name: statement.excluded[name]
                    for name in (
                        "bucket_seconds",
                        "in_count",
                        "out_count",
                        "max_occupancy",
                        "occupied_seconds",
                    )
                },
            )
        )
    return BatchResult(accepted=len(buckets) - len(rejected), rejected=rejected)
