import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import AttendanceSource, AttendanceStatus, ReviewStatus
from cctv_contracts.enums import EventStatus, Severity


class Event(TenantScoped, Base):
    """One detection (plan §3). `id` comes from the edge and is the idempotency key."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    # No single-column index: every events index below already starts with tenant_id.
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    branch_id: Mapped[uuid.UUID]
    camera_id: Mapped[uuid.UUID]
    zone_id: Mapped[uuid.UUID | None]
    edge_device_id: Mapped[uuid.UUID | None]
    feature_key: Mapped[str] = mapped_column(ForeignKey("features.key"))
    event_type: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    confidence: Mapped[float]
    staff_id: Mapped[uuid.UUID | None]
    track_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_key: Mapped[str | None] = mapped_column(String(512))
    clip_key: Mapped[str | None] = mapped_column(String(512))
    attributes: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    review_status: Mapped[str] = mapped_column(String(15), server_default=ReviewStatus.NEW.value)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None]
    review_note: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches"),
        tenant_fk("camera_id", "cameras"),
        tenant_fk("zone_id", "zones"),
        tenant_fk("edge_device_id", "edge_devices"),
        tenant_fk("staff_id", "staff"),
        UniqueConstraint("id", "tenant_id"),
        check_in("severity", Severity),
        check_in("status", EventStatus),
        check_in("review_status", ReviewStatus),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ended_after_started"),
        # Search filters from plan §3.6.
        Index("ix_events_tenant_started", "tenant_id", text("started_at DESC")),
        Index(
            "ix_events_tenant_feature_started", "tenant_id", "feature_key", text("started_at DESC")
        ),
        Index("ix_events_tenant_camera_started", "tenant_id", "camera_id", text("started_at DESC")),
        Index("ix_events_tenant_branch_started", "tenant_id", "branch_id", text("started_at DESC")),
        Index(
            "ix_events_tenant_staff_started",
            "tenant_id",
            "staff_id",
            text("started_at DESC"),
            postgresql_where=text("staff_id IS NOT NULL"),
        ),
        # Review queue: unreviewed events only.
        Index(
            "ix_events_tenant_unreviewed",
            "tenant_id",
            text("started_at DESC"),
            postgresql_where=text("review_status = 'new'"),
        ),
    )


class AttendanceRecord(UUIDPk, TenantScoped, Base):
    """One staff member's work day (plan 5.4).

    `first_seen_at` / `last_seen_at` always follow the face sightings from the edge. The
    effective `check_in_at`, `check_out_at` and `status` are derived from them while
    `source` is 'face'; a manager's correction sets `source` to 'manual' and they stay as
    corrected until the correction is removed.
    """

    __tablename__ = "attendance_records"

    branch_id: Mapped[uuid.UUID]
    staff_id: Mapped[uuid.UUID]
    work_date: Mapped[date]
    check_in_at: Mapped[datetime | None]
    check_out_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(10), server_default=AttendanceSource.FACE.value)
    first_seen_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    corrected_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches"),
        tenant_fk("staff_id", "staff", ondelete="CASCADE"),
        UniqueConstraint("staff_id", "work_date"),
        check_in("status", AttendanceStatus),
        check_in("source", AttendanceSource),
        CheckConstraint(
            "check_out_at IS NULL OR check_in_at IS NULL OR check_out_at >= check_in_at",
            name="check_out_after_check_in",
        ),
        Index("ix_attendance_tenant_date", "tenant_id", "work_date"),
    )


class CountBucket(TenantScoped, Base):
    """People in/out and occupancy per camera (and optional zone) per time bucket."""

    __tablename__ = "count_buckets"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    camera_id: Mapped[uuid.UUID]
    zone_id: Mapped[uuid.UUID | None]
    bucket_start: Mapped[datetime]
    bucket_seconds: Mapped[int] = mapped_column(SmallInteger, server_default="300")
    in_count: Mapped[int] = mapped_column(server_default="0")
    out_count: Mapped[int] = mapped_column(server_default="0")
    max_occupancy: Mapped[int | None]
    # Seconds with at least one person in the zone (e.g. the counter was staffed).
    occupied_seconds: Mapped[int | None]
    # Plan 10.2: line crossings left out because the person is staff; queue waits ended
    # in the bucket (how many, total and longest seconds).
    staff_crossings: Mapped[int | None]
    waits: Mapped[int | None]
    wait_total_seconds: Mapped[int | None]
    wait_max_seconds: Mapped[int | None]

    __table_args__ = (
        tenant_fk("camera_id", "cameras", ondelete="CASCADE"),
        tenant_fk("zone_id", "zones", ondelete="CASCADE"),
        UniqueConstraint(
            "camera_id", "zone_id", "bucket_start", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint("in_count >= 0 AND out_count >= 0", name="counts_non_negative"),
        CheckConstraint(
            "occupied_seconds IS NULL OR occupied_seconds BETWEEN 0 AND bucket_seconds",
            name="occupied_within_bucket",
        ),
        Index("ix_count_buckets_tenant_start", "tenant_id", "bucket_start"),
    )


class HeatmapBucket(TenantScoped, Base):
    """Where people (not staff) stood in a camera's view per hour (later phase: heatmaps):
    person-seconds per cell of a columns x rows grid over the picture, as [index, seconds]
    pairs for the cells with time in them."""

    __tablename__ = "heatmap_buckets"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    camera_id: Mapped[uuid.UUID]
    bucket_start: Mapped[datetime]
    bucket_seconds: Mapped[int] = mapped_column(SmallInteger, server_default="3600")
    columns: Mapped[int] = mapped_column(SmallInteger)
    rows: Mapped[int] = mapped_column(SmallInteger)
    cells: Mapped[list[list[int]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (
        tenant_fk("camera_id", "cameras", ondelete="CASCADE"),
        UniqueConstraint("camera_id", "bucket_start"),
        Index("ix_heatmap_buckets_tenant_start", "tenant_id", "bucket_start"),
    )
