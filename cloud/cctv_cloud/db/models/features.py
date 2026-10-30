import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import FeatureAvailability, FeatureScope


class Feature(Base):
    """Global feature catalogue (seeded by migrations, read-only for the app)."""

    __tablename__ = "features"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(40))
    availability: Mapped[str] = mapped_column(String(20))
    default_enabled: Mapped[bool] = mapped_column(server_default=text("false"))

    __table_args__ = (check_in("availability", FeatureAvailability),)


class FeatureSetting(UUIDPk, TenantScoped, Base):
    """On/off + parameters for a feature at tenant, branch or camera level.

    Resolution order: camera > branch > tenant > Feature.default_enabled.
    """

    __tablename__ = "feature_settings"

    scope: Mapped[str] = mapped_column(String(10))
    branch_id: Mapped[uuid.UUID | None]
    camera_id: Mapped[uuid.UUID | None]
    feature_key: Mapped[str] = mapped_column(ForeignKey("features.key"))
    enabled: Mapped[bool]
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        tenant_fk("camera_id", "cameras", ondelete="CASCADE"),
        UniqueConstraint(
            "tenant_id", "feature_key", "branch_id", "camera_id", postgresql_nulls_not_distinct=True
        ),
        check_in("scope", FeatureScope),
        CheckConstraint(
            "(scope = 'tenant' AND branch_id IS NULL AND camera_id IS NULL)"
            " OR (scope = 'branch' AND branch_id IS NOT NULL AND camera_id IS NULL)"
            " OR (scope = 'camera' AND camera_id IS NOT NULL AND branch_id IS NULL)",
            name="scope_target",
        ),
    )
