"""People counts per camera and zone, aggregated on the edge into time buckets.

- Entrance lines (`entrance_line` zones): `in_count` / `out_count`.
- Area zones (counter, queue, ...): `max_occupancy` and `occupied_seconds` (time with at
  least one person in the zone), e.g. how long the counter was staffed.
- Staff are left out of customer counts (entrance lines, queue and dining zones): people
  seen in a staff area, kitchen or counter zone of the same camera. `staff_crossings` says
  how many line crossings were left out for that reason.
- Queue zones: `waits` people left the queue in the bucket, together `wait_total_seconds`
  in it, the longest `wait_max_seconds` (time each person's track spent in the zone).

The box sends each bucket once it is complete, with its totals; resending the same
bucket replaces it, so retries are harmless.
"""

from uuid import UUID

from pydantic import Field, model_validator

from cctv_contracts.v1.common import ContractModel, UtcDatetime

MAX_BUCKETS_PER_BATCH = 500


class CountBucketIn(ContractModel):
    camera_id: UUID
    zone_id: UUID | None = None
    bucket_start: UtcDatetime
    bucket_seconds: int = Field(default=300, ge=60, le=3600)
    in_count: int = Field(default=0, ge=0)
    out_count: int = Field(default=0, ge=0)
    max_occupancy: int | None = Field(default=None, ge=0)
    occupied_seconds: int | None = Field(default=None, ge=0)
    staff_crossings: int | None = Field(default=None, ge=0)
    waits: int | None = Field(default=None, ge=0)
    wait_total_seconds: int | None = Field(default=None, ge=0)
    wait_max_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_bucket(self) -> "CountBucketIn":
        if int(self.bucket_start.timestamp()) % self.bucket_seconds or (
            self.bucket_start.microsecond
        ):
            raise ValueError("bucket_start must be a multiple of bucket_seconds (UTC)")
        if self.occupied_seconds is not None and self.occupied_seconds > self.bucket_seconds:
            raise ValueError("occupied_seconds is longer than the bucket")
        return self


class CountBatch(ContractModel):
    buckets: list[CountBucketIn] = Field(min_length=1, max_length=MAX_BUCKETS_PER_BATCH)
