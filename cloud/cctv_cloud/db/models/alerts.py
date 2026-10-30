import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import AlertChannel, DeliveryStatus
from cctv_contracts.enums import Severity

_CHANNEL_VALUES = ", ".join(f"'{c.value}'" for c in AlertChannel)


class AlertSubscription(UUIDPk, TenantScoped, Base):
    """Which alerts a user receives. NULL feature/branch means "all"."""

    __tablename__ = "alert_subscriptions"

    user_id: Mapped[uuid.UUID]
    feature_key: Mapped[str | None] = mapped_column(ForeignKey("features.key"))
    branch_id: Mapped[uuid.UUID | None]
    min_severity: Mapped[str] = mapped_column(String(10), server_default=Severity.WARNING.value)
    channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), server_default=text("ARRAY['web']::varchar(20)[]")
    )
    quiet_from: Mapped[time | None]
    quiet_until: Mapped[time | None]
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))

    __table_args__ = (
        tenant_fk("user_id", "users", ondelete="CASCADE"),
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        check_in("min_severity", Severity),
        CheckConstraint(
            f"channels <@ ARRAY[{_CHANNEL_VALUES}]::varchar(20)[]", name="channels_valid"
        ),
    )


class AlertDelivery(TenantScoped, Base):
    """One alert sent to one user on one channel; latency_ms proves the < 10 s target."""

    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10), server_default=DeliveryStatus.QUEUED.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
    latency_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        tenant_fk("event_id", "events", ondelete="CASCADE"),
        tenant_fk("user_id", "users", ondelete="CASCADE"),
        check_in("channel", AlertChannel),
        check_in("status", DeliveryStatus),
        Index("ix_alert_deliveries_event", "event_id"),
    )


class AuditLog(Base):
    """Who viewed or exported what (plan §7.3). The app role may only insert and read."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # NULL for platform-level actions by super-admins.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50))
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_audit_logs_tenant_at", "tenant_id", text("at DESC")),)
