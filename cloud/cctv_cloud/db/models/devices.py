import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, CreatedAt, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import DeviceStatus, IncidentKind
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
    # SHA-256 of the device token; the token itself is returned once, at pairing.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Tokens are rotated (plan 7.1); the replaced one works until previous_token_expires_at.
    token_issued_at: Mapped[datetime | None]
    previous_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    previous_token_expires_at: Mapped[datetime | None]
    # One-time pairing code shown in the admin panel and typed in on the box.
    pairing_code_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    pairing_expires_at: Mapped[datetime | None]
    # From the last heartbeat (plan 11.4: monitoring).
    uptime_seconds: Mapped[int | None]
    cpu_percent: Mapped[float | None]
    gpu_percent: Mapped[float | None]
    temperature_c: Mapped[float | None]
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    outbox_pending: Mapped[int | None]

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
    # The box's count of stream restarts in its last heartbeat (it counts up from 0 after
    # each start of the service), to work out new restarts.
    stream_restarts: Mapped[int] = mapped_column(server_default="0")

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


class CameraHealthMinute(TenantScoped, Base):
    """Stream health per camera and minute, from the edge heartbeats (plan 9.2: the setup
    test, 48 h without dropped streams). A minute without a row had no heartbeat."""

    __tablename__ = "camera_health_minutes"

    camera_id: Mapped[uuid.UUID]
    minute: Mapped[datetime]
    samples: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    degraded: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    offline: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    restarts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    min_fps: Mapped[float | None]

    __table_args__ = (
        PrimaryKeyConstraint("camera_id", "minute"),
        tenant_fk("camera_id", "cameras", ondelete="CASCADE"),
    )


class HealthIncident(UUIDPk, TenantScoped, Base):
    """Something wrong with a branch's system (plan 11.4): a box offline, a camera stream
    down, a disk filling, a box too hot, uploads backing up. Opened and resolved by the
    worker's health check; on-call is told once it has lasted a few minutes."""

    __tablename__ = "health_incidents"

    branch_id: Mapped[uuid.UUID]
    device_id: Mapped[uuid.UUID | None]
    camera_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(500))
    value: Mapped[float | None]  # e.g. GB free, degrees C
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    notified_at: Mapped[datetime | None]  # on-call told it opened
    resolved_notified_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        tenant_fk("device_id", "edge_devices", ondelete="CASCADE"),
        tenant_fk("camera_id", "cameras", ondelete="CASCADE"),
        check_in("kind", IncidentKind),
        # At most one open incident per kind and box or camera.
        Index(
            "uq_health_incidents_open",
            "kind",
            "device_id",
            "camera_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_health_incidents_tenant_opened", "tenant_id", text("opened_at DESC")),
    )
