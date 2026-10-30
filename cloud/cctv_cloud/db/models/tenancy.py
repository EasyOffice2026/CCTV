import uuid
from datetime import datetime, time

from sqlalchemy import CheckConstraint, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import TenantStatus


class Tenant(UUIDPk, CreatedAt, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default=TenantStatus.ACTIVE.value)
    # Plan §6 defaults; continuous footage itself stays on the client's NVR.
    retention_footage_days: Mapped[int] = mapped_column(server_default="30")
    retention_event_days: Mapped[int] = mapped_column(server_default="90")

    __table_args__ = (
        check_in("status", TenantStatus),
        CheckConstraint(
            "retention_footage_days BETWEEN 1 AND 3650", name="retention_footage_range"
        ),
        CheckConstraint("retention_event_days BETWEEN 1 AND 3650", name="retention_event_range"),
    )


class Branch(UUIDPk, CreatedAt, TenantScoped, Base):
    __tablename__ = "branches"

    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Kuwait")
    address: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("tenant_id", "name"),
    )


class WorkingHours(UUIDPk, TenantScoped, Base):
    """Opening hours; several rows per weekday allow split shifts.

    closes_at <= opens_at means the branch closes after midnight.
    """

    __tablename__ = "working_hours"

    branch_id: Mapped[uuid.UUID]
    weekday: Mapped[int] = mapped_column(SmallInteger)  # 0 = Monday
    opens_at: Mapped[time]
    closes_at: Mapped[time]

    __table_args__ = (
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
    )


class Shift(UUIDPk, TenantScoped, Base):
    __tablename__ = "shifts"

    branch_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(100))
    starts_at: Mapped[time]
    ends_at: Mapped[time]
    grace_minutes: Mapped[int] = mapped_column(SmallInteger, server_default="10")
    updated_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("branch_id", "name"),
        CheckConstraint("grace_minutes BETWEEN 0 AND 240", name="grace_minutes_range"),
    )
