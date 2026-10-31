import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import AlertChannel, Locale, StaffStatus, UserRole


class Staff(UUIDPk, CreatedAt, TenantScoped, Base):
    __tablename__ = "staff"

    branch_id: Mapped[uuid.UUID]
    shift_id: Mapped[uuid.UUID | None]
    employee_code: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), server_default=StaffStatus.ACTIVE.value)
    # Face enrolment is refused by a DB trigger until this is set (plan §7.5).
    consent_signed_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches"),
        tenant_fk("shift_id", "shifts"),
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("tenant_id", "employee_code"),
        check_in("status", StaffStatus),
    )


class FaceTemplate(UUIDPk, CreatedAt, TenantScoped, Base):
    """Encrypted face embedding (never a photo). Deleted by trigger when staff leave."""

    __tablename__ = "face_templates"

    staff_id: Mapped[uuid.UUID]
    embedding_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(200))  # KMS key used to encrypt
    model_version: Mapped[str] = mapped_column(String(50))

    __table_args__ = (tenant_fk("staff_id", "staff", ondelete="CASCADE"),)


class User(UUIDPk, CreatedAt, Base):
    __tablename__ = "users"

    # NULL only for super-admins (our team), which the RLS-bound app role can't see.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Last accepted TOTP time step, so a code can't be used twice.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    last_login_at: Mapped[datetime | None]
    # Login lockout after repeated wrong passwords.
    failed_logins: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    locked_until: Mapped[datetime | None]
    # Password-reset links embed this, so they stop working once the password changes.
    password_changed_at: Mapped[datetime | None]
    # Alerts and reports (plan 6.3, 6.5): WhatsApp number (E.164) and when the user agreed
    # to receive WhatsApp messages; the language of their messages.
    phone: Mapped[str | None] = mapped_column(String(20))
    whatsapp_opt_in_at: Mapped[datetime | None]
    locale: Mapped[str] = mapped_column(String(5), server_default=Locale.EN.value)
    # Where to send a link when a branch report is ready (email, whatsapp).
    report_channels: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), server_default=text("'{}'::varchar(20)[]")
    )

    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
        check_in("role", UserRole),
        check_in("locale", Locale),
        CheckConstraint("(role = 'super_admin') = (tenant_id IS NULL)", name="role_tenant"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{6,14}$'", name="phone_e164"),
        CheckConstraint(
            f"report_channels <@ ARRAY['{AlertChannel.EMAIL.value}',"
            f" '{AlertChannel.WHATSAPP.value}']::varchar(20)[]",
            name="report_channels_valid",
        ),
    )


class UserBranchAccess(TenantScoped, Base):
    __tablename__ = "user_branch_access"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)

    __table_args__ = (
        tenant_fk("user_id", "users", ondelete="CASCADE"),
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
    )


class UserSession(UUIDPk, CreatedAt, Base):
    """A login. The refresh token names the session and its current `refresh_jti`;
    each refresh rotates the jti, and reusing an old one revokes the session."""

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    refresh_jti: Mapped[str] = mapped_column(String(64))
    last_used_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (Index("ix_user_sessions_user", "user_id"),)


class ApiKey(UUIDPk, CreatedAt, TenantScoped, Base):
    """Key for integrations (POS, HRM). Shown once; only a hash is stored."""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(40)))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("id", "tenant_id"),)
