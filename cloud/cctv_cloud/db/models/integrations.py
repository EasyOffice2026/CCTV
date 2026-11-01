import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import DeliveryStatus, SsoAlgorithm, WebhookTopic
from cctv_contracts.enums import Severity

_TOPIC_VALUES = ", ".join(f"'{t.value}'" for t in WebhookTopic)


class SsoIntegration(UUIDPk, CreatedAt, TenantScoped, Base):
    """Sign-in from the client's operational system (plan 8.1). It signs a short-lived JWT
    for one of its users; a token from `issuer` that verifies with this key starts a
    session for the user with the same email in this tenant. Set up by our team."""

    __tablename__ = "sso_integrations"

    name: Mapped[str] = mapped_column(String(100))
    # The tokens' `iss`; unique across tenants, it is how a token finds its tenant.
    issuer: Mapped[str] = mapped_column(String(200), unique=True)
    algorithm: Mapped[str] = mapped_column(String(10))
    secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)  # HS256
    public_key: Mapped[str | None] = mapped_column(Text)  # RS256/ES256, PEM
    # Owners must use 2FA here; with this on, their tokens must say (`amr`) that the
    # operational system used a second factor too.
    require_mfa: Mapped[bool] = mapped_column(server_default=text("true"))
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        check_in("algorithm", SsoAlgorithm),
        CheckConstraint(
            "(algorithm = 'HS256') = (secret_encrypted IS NOT NULL)"
            " AND (algorithm = 'HS256') = (public_key IS NULL)",
            name="key_matches_algorithm",
        ),
    )


class SsoTokenUse(TenantScoped, Base):
    """Sign-in tokens already used (by `jti`), so a copied token can't be used again.
    Rows are only needed until the token expires."""

    __tablename__ = "sso_token_uses"

    integration_id: Mapped[uuid.UUID]
    jti: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime]

    __table_args__ = (
        PrimaryKeyConstraint("integration_id", "jti"),
        tenant_fk("integration_id", "sso_integrations", ondelete="CASCADE"),
    )


class Webhook(UUIDPk, CreatedAt, TenantScoped, Base):
    """Events and reports pushed to an integration (plan 8.4), signed with `secret`."""

    __tablename__ = "webhooks"

    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(1000))
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    topics: Mapped[list[str]] = mapped_column(ARRAY(String(30)))
    branch_id: Mapped[uuid.UUID | None]  # NULL: every branch
    min_severity: Mapped[str] = mapped_column(String(10), server_default=Severity.INFO.value)
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_failure_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        check_in("min_severity", Severity),
        CheckConstraint(
            f"topics <@ ARRAY[{_TOPIC_VALUES}]::varchar(30)[] AND cardinality(topics) > 0",
            name="topics_valid",
        ),
    )


class WebhookDelivery(TenantScoped, Base):
    """One message to one webhook, retried with backoff until it is accepted (2xx)."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    webhook_id: Mapped[uuid.UUID]
    topic: Mapped[str] = mapped_column(String(30))
    # One message per webhook and thing (e.g. "event.new:<event id>").
    dedup_key: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(10), server_default=DeliveryStatus.QUEUED.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Claimed by moving it forward, as for alert deliveries.
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    sent_at: Mapped[datetime | None]
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        tenant_fk("webhook_id", "webhooks", ondelete="CASCADE"),
        UniqueConstraint("webhook_id", "dedup_key"),
        check_in("status", DeliveryStatus),
        Index(
            "ix_webhook_deliveries_queued",
            "next_attempt_at",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_webhook_deliveries_webhook", "webhook_id", text("id DESC")),
    )
