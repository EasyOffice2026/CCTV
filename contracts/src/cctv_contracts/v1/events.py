from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from cctv_contracts.enums import EVENT_FEATURE, EventStatus, EventType, FeatureKey, Severity
from cctv_contracts.v1.common import ContractModel, MediaKey, UtcDatetime

MAX_EVENTS_PER_BATCH = 100


class EventIn(ContractModel):
    """One detection sent by the edge.

    `event_id` is generated on the edge and is the idempotency key: resending the
    same id (retry, or an update to an ongoing event) updates the stored event.
    """

    event_id: UUID
    camera_id: UUID
    zone_id: UUID | None = None
    feature: FeatureKey
    event_type: EventType
    severity: Severity = Severity.WARNING
    status: EventStatus = EventStatus.OPEN
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    confidence: float = Field(ge=0, le=1)
    track_id: str | None = Field(default=None, max_length=64)
    staff_id: UUID | None = None
    snapshot_key: MediaKey
    clip_key: MediaKey | None = None
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_consistency(self) -> "EventIn":
        if EVENT_FEATURE[self.event_type] is not self.feature:
            raise ValueError(
                f"event_type {self.event_type.value!r} does not belong to"
                f" feature {self.feature.value!r}"
            )
        if self.status is EventStatus.CLOSED:
            if self.ended_at is None:
                raise ValueError("closed events need ended_at")
            # The brief requires a clip for every detection; it is finished after the post-roll.
            if self.clip_key is None:
                raise ValueError("closed events need clip_key")
        elif self.ended_at is not None:
            raise ValueError("open events must not have ended_at")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at is before started_at")
        return self


class EventBatch(ContractModel):
    events: list[EventIn] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class EventResult(ContractModel):
    event_id: UUID
    accepted: bool
    error: str | None = None


class EventBatchResponse(ContractModel):
    results: list[EventResult]
