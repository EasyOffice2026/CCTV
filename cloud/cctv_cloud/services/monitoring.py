"""Monitoring and on-call (plan 11.4).

The worker's health check runs every minute for every tenant. It compares what the boxes
report (heartbeats: status, camera health, disk, temperature, upload backlog) with the
limits in the settings and keeps `health_incidents` in step: a new problem opens an
incident, a problem that went away resolves it. One open incident per kind and box (or
camera). A camera's stream is only judged while its branch has a box online; when the box
itself is offline, that one incident covers its cameras.

On-call is told by email and/or a chat webhook (Slack or Teams incoming webhook, JSON
`{"text": ...}`), once an incident has lasted `monitor_notify_after_minutes` (so a box
that restarts doesn't page anyone) and again when it is resolved. Notifications are sent
after the check's transaction from rows not yet marked as sent, so a crash between the two
sends them on the next run instead of losing them.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus, IncidentKind
from cctv_cloud.db.models import Branch, Camera, EdgeDevice, HealthIncident, Tenant
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services.mailer import Mailer, get_mailer
from cctv_cloud.services.webhooks import check_url
from cctv_contracts.enums import CameraHealth

log = logging.getLogger(__name__)

TITLES = {
    IncidentKind.DEVICE_OFFLINE: "Edge box offline",
    IncidentKind.STREAM_DOWN: "Camera stream down",
    IncidentKind.DISK_LOW: "Edge box disk almost full",
    IncidentKind.TEMPERATURE_HIGH: "Edge box too hot",
    IncidentKind.SYNC_BACKLOG: "Edge box can't upload",
}

_Key = tuple[str, uuid.UUID | None, uuid.UUID | None]


@dataclass(frozen=True)
class Problem:
    branch_id: uuid.UUID
    detail: str
    value: float | None = None


def find_problems(db: Session, now: datetime) -> dict[_Key, Problem]:
    """What is wrong right now in the session's tenant."""
    settings = get_settings()
    problems: dict[_Key, Problem] = {}
    devices = db.scalars(
        select(EdgeDevice).where(
            EdgeDevice.status.in_([DeviceStatus.ONLINE.value, DeviceStatus.OFFLINE.value])
        )
    ).all()
    online_branches = set()
    for d in devices:
        if d.status == DeviceStatus.OFFLINE.value:
            seen = f"{d.last_seen_at:%Y-%m-%d %H:%M} UTC" if d.last_seen_at else "never"
            problems[(IncidentKind.DEVICE_OFFLINE.value, d.id, None)] = Problem(
                d.branch_id, f"{d.name}: no heartbeat since {seen}"
            )
            continue
        online_branches.add(d.branch_id)
        free_gb = d.disk_free_bytes / 1024**3 if d.disk_free_bytes is not None else None
        if free_gb is not None and free_gb < settings.monitor_disk_free_gb:
            problems[(IncidentKind.DISK_LOW.value, d.id, None)] = Problem(
                d.branch_id, f"{d.name}: {free_gb:.1f} GB free", round(free_gb, 1)
            )
        if d.temperature_c is not None and d.temperature_c >= settings.monitor_temperature_c:
            problems[(IncidentKind.TEMPERATURE_HIGH.value, d.id, None)] = Problem(
                d.branch_id, f"{d.name}: {d.temperature_c:.0f} °C", d.temperature_c
            )
        if d.outbox_pending is not None and d.outbox_pending >= settings.monitor_sync_backlog:
            problems[(IncidentKind.SYNC_BACKLOG.value, d.id, None)] = Problem(
                d.branch_id, f"{d.name}: {d.outbox_pending} items waiting", d.outbox_pending
            )
    stale = now - timedelta(minutes=settings.monitor_stream_down_minutes)
    if online_branches:
        for camera in db.scalars(
            select(Camera).where(
                Camera.enabled,
                Camera.branch_id.in_(online_branches),
                Camera.health == CameraHealth.OFFLINE.value,
                or_(Camera.last_seen_at.is_(None), Camera.last_seen_at < stale),
            )
        ):
            last = f"{camera.last_seen_at:%Y-%m-%d %H:%M} UTC" if camera.last_seen_at else "never"
            problems[(IncidentKind.STREAM_DOWN.value, None, camera.id)] = Problem(
                camera.branch_id, f"{camera.name}: no frames since {last}"
            )
    return problems


def check_tenant(db: Session, tenant_id: uuid.UUID, now: datetime) -> tuple[int, int]:
    """Open and resolve the tenant's incidents. Returns (opened, resolved)."""
    problems = find_problems(db, now)
    open_incidents = {
        (i.kind, i.device_id, i.camera_id): i
        for i in db.scalars(select(HealthIncident).where(HealthIncident.resolved_at.is_(None)))
    }
    opened = resolved = 0
    for key, problem in problems.items():
        incident = open_incidents.get(key)
        if incident is not None:
            incident.detail, incident.value = problem.detail[:500], problem.value
            continue
        kind, device_id, camera_id = key
        db.add(
            HealthIncident(
                tenant_id=tenant_id,
                branch_id=problem.branch_id,
                device_id=device_id,
                camera_id=camera_id,
                kind=kind,
                detail=problem.detail[:500],
                value=problem.value,
                opened_at=now,
            )
        )
        opened += 1
    for key, incident in open_incidents.items():
        if key not in problems:
            incident.resolved_at = now
            resolved += 1
    return opened, resolved


# --- on-call -------------------------------------------------------------------------------


class Pager(Protocol):
    def page(self, subject: str, body: str) -> None: ...


class OnCall:
    """Email to `oncall_emails` and a post to `oncall_webhook_url`, whichever are set."""

    def __init__(self, mailer: Mailer) -> None:
        self.mailer = mailer

    def page(self, subject: str, body: str) -> None:
        settings = get_settings()
        for address in settings.oncall_emails:
            self.mailer.send(address, subject, body)
        url = settings.oncall_webhook_url.get_secret_value()
        if url:
            check_url(url, resolve=True)
            response = httpx.post(
                url, json={"text": f"{subject}\n{body}"}, timeout=10, follow_redirects=False
            )
            response.raise_for_status()
        if not settings.oncall_emails and not url:
            log.warning("on-call: %s (no CCTV_ONCALL_EMAILS or webhook set)", subject)


def get_pager() -> Pager:
    return OnCall(get_mailer())


def _message(incident: HealthIncident, tenant: str, branch: str, resolved: bool) -> tuple:
    kind = IncidentKind(incident.kind)
    if resolved:
        assert incident.resolved_at is not None  # noqa: S101
        lasted = incident.resolved_at - incident.opened_at
        minutes = int(lasted.total_seconds() // 60)
        subject = f"[CCTV] Resolved: {TITLES[kind]} at {tenant} / {branch}"
        body = f"{incident.detail}\nLasted {minutes} min."
    else:
        subject = f"[CCTV] {TITLES[kind]} at {tenant} / {branch}"
        body = (
            f"{incident.detail}\nSince {incident.opened_at:%Y-%m-%d %H:%M} UTC.\n"
            f"See docs/runbooks/edge-recovery.md#{kind.value.replace('_', '-')}"
        )
    return subject, body


def notify_tenant(db: Session, pager: Pager, now: datetime) -> int:
    """Tell on-call about incidents that have lasted long enough, and about resolutions of
    incidents they were told about. Short ones are closed without a word. Returns pages."""
    delay = timedelta(minutes=get_settings().monitor_notify_after_minutes)
    tenant = db.scalar(select(Tenant.name)) or ""
    branches = dict(db.execute(select(Branch.id, Branch.name)).all())
    sent = 0
    for incident in db.scalars(
        select(HealthIncident).where(
            or_(
                HealthIncident.notified_at.is_(None),
                HealthIncident.resolved_notified_at.is_(None)
                & HealthIncident.resolved_at.is_not(None),
            )
        )
    ):
        branch = branches.get(incident.branch_id, "")
        if incident.notified_at is None:
            if incident.resolved_at is not None:  # over before anyone needed to know
                incident.notified_at = incident.resolved_notified_at = now
                continue
            if now - incident.opened_at < delay:
                continue
            pager.page(*_message(incident, tenant, branch, resolved=False))
            incident.notified_at = now
            sent += 1
        elif incident.resolved_at is not None:
            pager.page(*_message(incident, tenant, branch, resolved=True))
            incident.resolved_notified_at = now
            sent += 1
    return sent


def check_all(tenant_ids: list[uuid.UUID], pager: Pager, now: datetime | None = None) -> int:
    """Worker: every tenant's incidents, then on-call. Returns the pages sent."""
    now = now or datetime.now(timezone.utc)
    pages = 0
    for tenant_id in tenant_ids:
        try:
            with tenant_session(tenant_id) as db:
                opened, resolved = check_tenant(db, tenant_id, now)
            if opened or resolved:
                log.info("tenant %s: %d incidents opened, %d resolved", tenant_id, opened, resolved)
            with tenant_session(tenant_id) as db:
                pages += notify_tenant(db, pager, now)
        except Exception:
            log.exception("health check failed for tenant %s", tenant_id)
    return pages


def platform_counts() -> list[tuple[str, str, int]]:
    """(metric, label, value) across tenants, for /metrics."""
    with tenant_session(None) as db:
        rows = db.execute(text("SELECT metric, label, value FROM platform_monitoring()")).all()
    return [(r.metric, r.label, r.value) for r in rows]


def prometheus(counts: list[tuple[str, str, int]]) -> str:
    """Prometheus text format."""
    labels = {
        "edge_devices": "status",
        "cameras": "health",
        "incidents_open": "kind",
        "tenants": "status",
    }
    lines = ["# HELP cctv_up The API answers.", "# TYPE cctv_up gauge", "cctv_up 1"]
    seen: set[str] = set()
    for metric, label, value in sorted(counts):
        name = f"cctv_{metric}"
        if name not in seen:
            lines.append(f"# TYPE {name} gauge")
            seen.add(name)
        if metric in labels:
            lines.append(f'{name}{{{labels[metric]}="{label}"}} {value}')
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
