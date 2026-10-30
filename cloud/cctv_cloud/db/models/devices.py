import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, SmallInteger, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import DeviceStatus
from cctv_contracts.enums import CameraHealth, CameraRole, ZoneType


class EdgeDevice(UUIDPk, CreatedAt, TenantScoped, Base):
    __tablename__ = "edge_devices"

    branch_id: Mapped[uuid.UUID]
    serial: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), server_default=DeviceStatus.PENDING.value)
    software_version: Mapped[str | None] = mapped_column(String(50))
    config_version_applied: Mapped[int | None]
    last_seen_at: Mapped[datetime | None]
    # Hash of the device token; the token itself is shown once at pairing.
    token_hash: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        tenant_fk("branch_id", "branches"),
        UniqueConstraint("id", "tenant_id"),
        check_in("status", DeviceStatus),
    )


class Nvr(UUIDPk, CreatedAt, TenantScoped, Base):
    __tablename__ = "nvrs"

    branch_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(100))
    vendor: Mapped[str] = mapped_column(String(50), server_default="hikvision")
    model: Mapped[str | None] = mapped_column(String(100))
    host: Mapped[str] = mapped_column(String(255))
    http_port: Mapped[int] = mapped_column(server_default="80")
    rtsp_port: Mapped[int] = mapped_column(server_default="554")
    # Name of the credential stored on the edge device (never the password itself).
    credentials_ref: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        tenant_fk("branch_id", "branches"),
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("branch_id", "name"),
    )


class Camera(UUIDPk, CreatedAt, TenantScoped, Base):
    __tablename__ = "cameras"

    branch_id: Mapped[uuid.UUID]
    nvr_id: Mapped[uuid.UUID | None]
    channel: Mapped[int | None] = mapped_column(SmallInteger)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30), server_default=CameraRole.OTHER.value)
    rtsp_main: Mapped[str] = mapped_column(String(500))
    rtsp_sub: Mapped[str | None] = mapped_column(String(500))
    credentials_ref: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    health: Mapped[str] = mapped_column(String(20), server_default=CameraHealth.OFFLINE.value)
    last_seen_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches"),
        tenant_fk("nvr_id", "nvrs"),
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("branch_id", "name"),
        UniqueConstraint("nvr_id", "channel"),
        check_in("role", CameraRole),
        check_in("health", CameraHealth),
        CheckConstraint(
            "rtsp_main NOT LIKE '%@%' AND (rtsp_sub IS NULL OR rtsp_sub NOT LIKE '%@%')",
            name="rtsp_no_credentials",
        ),
    )


class Zone(UUIDPk, CreatedAt, TenantScoped, Base):
    __tablename__ = "zones"

    camera_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(100))
    zone_type: Mapped[str] = mapped_column(String(30))
    # [[x, y], ...] normalised to 0..1; validated by cctv_contracts.v1.ZoneConfig.
    points: Mapped[list[list[float]]] = mapped_column(JSONB)
    updated_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("camera_id", "cameras", ondelete="CASCADE"),
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("camera_id", "name"),
        check_in("zone_type", ZoneType),
    )
