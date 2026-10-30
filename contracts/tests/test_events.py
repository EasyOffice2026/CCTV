from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cctv_contracts.enums import EVENT_FEATURE, EventStatus, EventType, FeatureKey
from cctv_contracts.v1 import MAX_EVENTS_PER_BATCH, EventBatch, EventIn, UploadUrlRequest

KUWAIT = timezone(timedelta(hours=3))


def make_event(**overrides):
    data = {
        "event_id": str(uuid4()),
        "camera_id": str(uuid4()),
        "feature": "security",
        "event_type": "after_hours_person",
        "started_at": "2026-10-30T02:15:00+03:00",
        "confidence": 0.91,
        "snapshot_key": "t/abc/events/e1/snapshot.jpg",
    }
    data.update(overrides)
    return EventIn.model_validate(data)


def test_valid_open_event_is_normalised_to_utc():
    event = make_event()
    assert event.status is EventStatus.OPEN
    assert event.started_at == datetime(2026, 10, 29, 23, 15, tzinfo=timezone.utc)
    assert event.started_at.utcoffset() == timedelta(0)


def test_closed_event_needs_end_time_and_clip():
    with pytest.raises(ValidationError, match="closed events need ended_at"):
        make_event(status="closed", clip_key="t/abc/c.mp4")
    with pytest.raises(ValidationError, match="closed events need clip_key"):
        make_event(status="closed", ended_at="2026-10-30T02:16:00+03:00")
    event = make_event(
        status="closed", ended_at="2026-10-30T02:16:00+03:00", clip_key="t/abc/c.mp4"
    )
    assert event.ended_at - event.started_at == timedelta(minutes=1)


def test_open_event_must_not_have_end_time():
    with pytest.raises(ValidationError, match="open events must not have ended_at"):
        make_event(ended_at="2026-10-30T02:16:00+03:00")


def test_end_before_start_rejected():
    with pytest.raises(ValidationError, match="ended_at is before started_at"):
        make_event(status="closed", ended_at="2026-10-30T02:00:00+03:00", clip_key="t/c.mp4")


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        make_event(started_at="2026-10-30T02:15:00")


def test_event_type_must_match_feature():
    with pytest.raises(ValidationError, match="does not belong to feature"):
        make_event(feature="cashier")


def test_snapshot_is_required():
    with pytest.raises(ValidationError, match="snapshot_key"):
        make_event(snapshot_key=None)


@pytest.mark.parametrize("key", ["../etc/passwd", "t/a/../b.jpg", "t/a b.jpg", ""])
def test_bad_media_keys_rejected(key):
    with pytest.raises(ValidationError):
        make_event(snapshot_key=key)


def test_confidence_range():
    with pytest.raises(ValidationError):
        make_event(confidence=1.2)


def test_every_event_type_has_a_feature():
    assert set(EVENT_FEATURE) == set(EventType)
    assert set(EVENT_FEATURE.values()) <= set(FeatureKey)


def test_batch_limits():
    with pytest.raises(ValidationError):
        EventBatch(events=[])
    too_many = [make_event() for _ in range(MAX_EVENTS_PER_BATCH + 1)]
    with pytest.raises(ValidationError):
        EventBatch(events=too_many)


def test_unknown_fields_are_ignored_for_forward_compatibility():
    event = make_event(future_field="x")
    assert not hasattr(event, "future_field")


def test_upload_request_checks_kind_and_size():
    base = {"event_id": str(uuid4()), "sha256": "a" * 64}
    UploadUrlRequest.model_validate(
        {**base, "kind": "clip", "content_type": "video/mp4", "size_bytes": 3_000_000}
    )
    with pytest.raises(ValidationError, match="must be image/jpeg"):
        UploadUrlRequest.model_validate(
            {**base, "kind": "snapshot", "content_type": "video/mp4", "size_bytes": 10}
        )
    with pytest.raises(ValidationError, match="larger than"):
        UploadUrlRequest.model_validate(
            {**base, "kind": "snapshot", "content_type": "image/jpeg", "size_bytes": 50_000_000}
        )
