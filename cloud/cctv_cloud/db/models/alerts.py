import uuid
from datetime import datetime, time
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
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import AlertChannel, AlertKind, DeliveryStatus
from cctv_contracts.enums import Severity

_CHANNEL_VALUES = ", ".join(f"'{c.value}'" for c in AlertChannel)


class AlertSubscription(UUIDPk, TenantScoped, Base):
    """An alert rule: which events a user is told about, and how. NULL feature/branch
    means "all". During quiet hours (branch time) only critical alerts are sent."""

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
    # Also send a message when the event ends.
    notify_resolved: Mapped[bool] = mapped_column(server_default=text("false"))

    __table_args__ = (
        tenant_fk("user_id", "users", ondelete="CASCADE"),
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        check_in("min_severity", Severity),
        CheckConstraint(
            f"channels <@ ARRAY[{_CHANNEL_VALUES}]::varchar(20)[]", name="channels_valid"
        ),
    )


class AlertDelivery(TenantScoped, Base):
    """One alert sent to one user on one channel (plan 6.4: at most one per event, user,
    channel and kind). latency_ms (event start to sent) proves the < 10 s target."""

    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(10), server_default=AlertKind.ALERT.value)
    status: Mapped[str] = mapped_column(String(10), server_default=DeliveryStatus.QUEUED.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Queued deliveries are sent from this time on. A sender claims one by moving it
    # forward, so two senders never send the same message at once.
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    sent_at: Mapped[datetime | None]
    latency_ms: Mapped[int | None]
    delivered_at: Mapped[datetime | None]
    # The channel's message id (WhatsApp), to match its delivery reports.
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        tenant_fk("event_id", "events", ondelete="CASCADE"),
        tenant_fk("user_id", "users", ondelete="CASCADE"),
        UniqueConstraint("event_id", "user_id", "channel", "kind"),
        check_in("channel", AlertChannel),
        check_in("kind", AlertKind),
        check_in("status", DeliveryStatus),
        Index(
            "ix_alert_deliveries_queued",
            "next_attempt_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_alert_deliveries_tenant_created", "tenant_id", "created_at"),
        Index(
            "ix_alert_deliveries_provider_message",
            "provider_message_id",
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
    )


class PushSubscription(UUIDPk, CreatedAt, TenantScoped, Base):
    """A browser that receives Web Push notifications for a user (plan 6.2)."""

    __tablename__ = "push_subscriptions"

    user_id: Mapped[uuid.UUID]
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    last_success_at: Mapped[datetime | None]

    __table_args__ = (tenant_fk("user_id", "users", ondelete="CASCADE"),)


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
