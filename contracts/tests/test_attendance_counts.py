import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cctv_contracts.v1 import (
    BatchResult,
    CountBatch,
    CountBucketIn,
    FaceTemplate,
    SightingBatch,
    StaffSighting,
)

BUCKET = datetime(2026, 10, 31, 12, 5, tzinfo=timezone.utc)


def test_sighting_round_trip_in_utc():
    kuwait = timezone(timedelta(hours=3))
    sighting = StaffSighting(
        staff_id=uuid.uuid4(),
        camera_id=uuid.uuid4(),
        seen_at=datetime(2026, 10, 31, 9, 0, tzinfo=kuwait),
        similarity=0.71,
    )
    assert sighting.seen_at == datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
    batch = SightingBatch(sightings=[sighting])
    assert SightingBatch.model_validate_json(batch.model_dump_json()) == batch


def test_sighting_needs_an_offset_and_a_valid_score():
    base = {"staff_id": uuid.uuid4(), "camera_id": uuid.uuid4(), "similarity": 0.5}
    with pytest.raises(ValidationError):
        StaffSighting(**base, seen_at=datetime(2026, 10, 31, 9, 0))  # naive
    with pytest.raises(ValidationError):
        StaffSighting(**{**base, "similarity": 1.5}, seen_at=BUCKET)
    with pytest.raises(ValidationError):
        SightingBatch(sightings=[])


def test_face_template_embedding_length():
    FaceTemplate(staff_id=uuid.uuid4(), model_version="sface-2021dec", embedding=[0.1] * 128)
    with pytest.raises(ValidationError):
        FaceTemplate(staff_id=uuid.uuid4(), model_version="x", embedding=[0.1] * 8)


def test_count_bucket_must_be_aligned():
    camera = uuid.uuid4()
    CountBucketIn(camera_id=camera, bucket_start=BUCKET, in_count=3, out_count=1)
    with pytest.raises(ValidationError, match="multiple of bucket_seconds"):
        CountBucketIn(camera_id=camera, bucket_start=BUCKET + timedelta(seconds=30))
    with pytest.raises(ValidationError, match="multiple of bucket_seconds"):
        CountBucketIn(camera_id=camera, bucket_start=BUCKET, bucket_seconds=3600)
    hourly = CountBucketIn(
        camera_id=camera, bucket_start=BUCKET.replace(minute=0), bucket_seconds=3600
    )
    assert hourly.bucket_seconds == 3600


def test_count_bucket_values():
    camera = uuid.uuid4()
    with pytest.raises(ValidationError):
        CountBucketIn(camera_id=camera, bucket_start=BUCKET, in_count=-1)
    with pytest.raises(ValidationError, match="longer than the bucket"):
        CountBucketIn(camera_id=camera, bucket_start=BUCKET, occupied_seconds=301)
    bucket = CountBucketIn(
        camera_id=camera,
        zone_id=uuid.uuid4(),
        bucket_start=BUCKET,
        max_occupancy=2,
        occupied_seconds=300,
    )
    assert CountBatch(buckets=[bucket]).buckets[0].occupied_seconds == 300


def test_batch_result_defaults():
    result = BatchResult.model_validate({"accepted": 2})
    assert result.rejected == []
