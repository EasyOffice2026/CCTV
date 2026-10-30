import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import StaffStatus, UserRole


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
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    last_login_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
        check_in("role", UserRole),
        CheckConstraint("(role = 'super_admin') = (tenant_id IS NULL)", name="role_tenant"),
    )


class UserBranchAccess(TenantScoped, Base):
    __tablename__ = "user_branch_access"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    branch_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)

    __table_args__ = (
        tenant_fk("user_id", "users", ondelete="CASCADE"),
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
    )
