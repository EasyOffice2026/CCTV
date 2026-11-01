"""Webhooks to integrations such as the client's operational system (plan 8.4).

New and closed events and finished reports are queued for every matching webhook in the
transaction that stores them (one `webhook_deliveries` row per webhook and thing), then
sent like alerts: right after the commit, with the worker retrying with backoff for up to
a day. Messages carry what happened and a link to it on the dashboard (sign-in needed);
no snapshots, clips or per-person data.

Each request is a JSON POST signed with the webhook's secret:

    X-CCTV-Webhook-Id   the message id (same on every retry; use it to drop duplicates)
    X-CCTV-Topic        event.new | event.closed | report.ready | ping
    X-CCTV-Timestamp    unix seconds
    X-CCTV-Signature    v1=<hex HMAC-SHA256 of "<timestamp>.<body>" with the secret>

Webhooks only go to public addresses (checked when sending, not only when saved).
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeliveryStatus, WebhookTopic
from cctv_cloud.db.models import (
    Branch,
    Camera,
    Event,
    Report,
    Webhook,
    WebhookDelivery,
    Zone,
)
from cctv_cloud.db.session import tenant_session
from cctv_cloud.security import _fernet, new_secret
from cctv_cloud.services import pilot
from cctv_cloud.services.alerts import SEVERITY_RANK, Change, event_url

log = logging.getLogger(__name__)

CLAIM_SECONDS = 60
EXPIRE_AFTER = timedelta(hours=24)
BATCH = 100
# Kept for the deliveries list on the dashboard, then removed by the retention job.
KEEP_DELIVERIES = timedelta(days=30)


# --- secrets, URLs, signatures ---------------------------------------------------------


def new_webhook_secret() -> str:
    return "whsec_" + new_secret(32)


def encrypt_secret(secret: str) -> bytes:
    return _fernet(b"webhook-secret").encrypt(secret.encode())


def decrypt_secret(encrypted: bytes) -> str:
    return _fernet(b"webhook-secret").decrypt(encrypted).decode()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


class WebhookError(Exception):
    def __init__(self, message: str, retry: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retry = retry
        self.status = status


def check_url(url: str, resolve: bool = False) -> None:
    """ValueError unless `url` is https to a public host. With `resolve`, every address
    the name resolves to must be public too (so a name can't point at our network)."""
    settings = get_settings()
    parts = urlsplit(url)
    allowed = ("https", "http") if settings.webhook_allow_private else ("https",)
    if parts.scheme not in allowed or not parts.hostname:
        raise ValueError("the webhook URL must be https://")
    if parts.username or parts.password:
        raise ValueError("put credentials in the signature check, not the URL")
    if settings.webhook_allow_private:
        return
    host = parts.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("the webhook host must be public") from None
        if not resolve:
            return
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ValueError(f"cannot resolve {host}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not all(a.is_global for a in addresses):
        raise ValueError("the webhook host must be public")


# --- queueing ----------------------------------------------------------------------------


def _matching(
    db: Session, topic: WebhookTopic, branch_id: uuid.UUID, severity: str | None = None
) -> list[Webhook]:
    hooks = db.scalars(
        select(Webhook).where(
            Webhook.enabled,
            Webhook.topics.any(topic.value),
            or_(Webhook.branch_id.is_(None), Webhook.branch_id == branch_id),
        )
    ).all()
    if severity is None:
        return list(hooks)
    rank = SEVERITY_RANK[severity]
    return [h for h in hooks if SEVERITY_RANK[h.min_severity] <= rank]


def _queue(
    db: Session, hooks: list[Webhook], topic: WebhookTopic, key: str, data: dict
) -> list[int]:
    ids = []
    for hook in hooks:
        delivery_id = db.scalar(
            insert(WebhookDelivery)
            .values(
                tenant_id=hook.tenant_id,
                webhook_id=hook.id,
                topic=topic.value,
                dedup_key=key,
                payload=data,
            )
            .on_conflict_do_nothing()
            .returning(WebhookDelivery.id)
        )
        if delivery_id is not None:
            ids.append(delivery_id)
    return ids


def event_data(db: Session, event: Event) -> dict[str, Any]:
    branch = db.get_one(Branch, event.branch_id)
    camera = db.get(Camera, event.camera_id)
    zone = db.get(Zone, event.zone_id) if event.zone_id else None
    return {
        "event_id": str(event.id),
        "branch_id": str(event.branch_id),
        "branch": branch.name,
        "camera_id": str(event.camera_id),
        "camera": camera.name if camera else None,
        "zone": zone.name if zone else None,
        "feature": event.feature_key,
        "event_type": event.event_type,
        "severity": event.severity,
        "status": event.status,
        "started_at": event.started_at.isoformat(),
        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
        "confidence": round(event.confidence, 3),
        "url": event_url(event.id),
    }


def queue_event(db: Session, event: Event, change: Change) -> list[int]:
    """In the ingest transaction: one message per matching webhook (new or closed)."""
    topic = {Change.NEW: WebhookTopic.EVENT_NEW, Change.CLOSED: WebhookTopic.EVENT_CLOSED}.get(
        change
    )
    if topic is None or pilot.branch_in_shadow(db, event.branch_id, datetime.now(timezone.utc)):
        return []  # nothing reaches the client during shadow mode (plan 9.4)
    hooks = _matching(db, topic, event.branch_id, event.severity)
    if not hooks:
        return []
    return _queue(db, hooks, topic, f"{topic.value}:{event.id}", event_data(db, event))


def queue_report(db: Session, report: Report, branch: Branch, url: str) -> list[int]:
    """When a scheduled report is made: one message per matching webhook."""
    if pilot.shadow_active(branch.shadow_until, datetime.now(timezone.utc)):
        return []
    hooks = _matching(db, WebhookTopic.REPORT_READY, branch.id)
    if not hooks:
        return []
    data = {
        "report_id": str(report.id),
        "branch_id": str(branch.id),
        "branch": branch.name,
        "period": report.period,
        "period_start": report.period_start.isoformat(),
        "period_end": report.period_end.isoformat(),
        "totals": report.data.get("totals", {}),  # branch totals; no per-person rows
        "url": url,
    }
    key = f"{WebhookTopic.REPORT_READY.value}:{report.id}:{report.generated_at.isoformat()}"
    return _queue(db, hooks, WebhookTopic.REPORT_READY, key[:100], data)


# --- sending -----------------------------------------------------------------------------


class WebhookPoster(Protocol):
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        """POST; returns the status code (2xx) or raises WebhookError."""


class HttpPoster:
    def post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        try:
            check_url(url, resolve=True)
        except ValueError as exc:
            raise WebhookError(str(exc), retry=False) from exc
        try:
            response = httpx.post(
                url,
                content=body,
                headers=headers,
                timeout=get_settings().webhook_timeout_seconds,
                follow_redirects=False,  # a redirect could point anywhere
            )
        except httpx.HTTPError as exc:
            raise WebhookError(f"{type(exc).__name__}: {exc}") from exc
        if 200 <= response.status_code < 300:
            return response.status_code
        retry = response.status_code >= 500 or response.status_code in (408, 425, 429)
        raise WebhookError(
            f"HTTP {response.status_code}: {response.text[:200]}",
            retry=retry,
            status=response.status_code,
        )


def get_webhook_poster() -> WebhookPoster:
    return HttpPoster()


def post_message(
    poster: WebhookPoster, url: str, secret: str, message_id: str, topic: str, data: dict
) -> int:
    body = json.dumps(
        {
            "id": message_id,
            "topic": topic,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    return poster.post(
        url,
        body,
        {
            "Content-Type": "application/json",
            "User-Agent": "cctv-cloud-webhooks/1",
            "X-CCTV-Webhook-Id": message_id,
            "X-CCTV-Topic": topic,
            "X-CCTV-Timestamp": str(timestamp),
            "X-CCTV-Signature": sign(secret, timestamp, body),
        },
    )


@dataclass
class _Job:
    delivery_id: int
    webhook_id: uuid.UUID
    attempts: int
    url: str
    secret: str
    message_id: str
    topic: str
    data: dict


def _claim(db: Session, now: datetime, ids: list[int] | None) -> list[_Job]:
    due = (
        select(WebhookDelivery.id)
        .where(
            WebhookDelivery.status == DeliveryStatus.QUEUED.value,
            WebhookDelivery.next_attempt_at <= now,
        )
        .order_by(WebhookDelivery.next_attempt_at)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    )
    if ids is not None:
        due = due.where(WebhookDelivery.id.in_(ids))
    claimed = list(
        db.scalars(
            update(WebhookDelivery)
            .where(WebhookDelivery.id.in_(due.scalar_subquery()))
            .values(
                attempts=WebhookDelivery.attempts + 1,
                next_attempt_at=now + timedelta(seconds=CLAIM_SECONDS),
            )
            .returning(WebhookDelivery.id)
        )
    )
    jobs = []
    rows = db.execute(
        select(WebhookDelivery, Webhook)
        .join(Webhook, Webhook.id == WebhookDelivery.webhook_id)
        .where(WebhookDelivery.id.in_(claimed))
    ).all()
    for delivery, hook in rows:
        if delivery.created_at < now - EXPIRE_AFTER or not hook.enabled:
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error = "expired" if hook.enabled else "webhook disabled"
            continue
        jobs.append(
            _Job(
                delivery_id=delivery.id,
                webhook_id=hook.id,
                attempts=delivery.attempts,
                url=hook.url,
                secret=decrypt_secret(hook.secret_encrypted),
                message_id=delivery.dedup_key,
                topic=delivery.topic,
                data=delivery.payload,
            )
        )
    return jobs


def dispatch(
    tenant_id: uuid.UUID,
    poster: WebhookPoster,
    ids: list[int] | None = None,
    now: datetime | None = None,
) -> int:
    """Send this tenant's due webhook messages (only `ids`, if given). Returns how many
    were accepted."""
    settings = get_settings()
    sent = 0
    while True:
        claim_time = now or datetime.now(timezone.utc)
        with tenant_session(tenant_id) as db:
            jobs = _claim(db, claim_time, ids)
        if not jobs:
            return sent
        for job in jobs:
            finished = datetime.now(timezone.utc)
            hook_update: dict[str, Any]
            try:
                status = post_message(
                    poster, job.url, job.secret, job.message_id, job.topic, job.data
                )
            except WebhookError as exc:
                error = str(exc)[:1000]
                outcome: dict[str, Any] = {"error": error, "response_status": exc.status}
                if exc.retry and job.attempts < settings.webhook_max_attempts:
                    backoff = min(30 * 2 ** (job.attempts - 1), 3600)
                    outcome["next_attempt_at"] = finished + timedelta(seconds=backoff)
                else:
                    outcome["status"] = DeliveryStatus.FAILED.value
                hook_update = {"last_failure_at": finished, "last_error": error}
            else:
                outcome = {
                    "status": DeliveryStatus.SENT.value,
                    "sent_at": finished,
                    "response_status": status,
                    "error": None,
                }
                hook_update = {"last_success_at": finished}
                sent += 1
            with tenant_session(tenant_id) as db:
                db.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.id == job.delivery_id)
                    .values(**outcome)
                )
                db.execute(
                    update(Webhook).where(Webhook.id == job.webhook_id).values(**hook_update)
                )
        if ids is not None:
            return sent


def dispatch_all(poster: WebhookPoster) -> int:
    """Worker: send every tenant's due webhook messages."""
    with tenant_session(None) as db:
        tenants = list(db.scalars(text("SELECT tenant_id FROM webhook_due_tenants()")))
    sent = 0
    for tenant_id in tenants:
        try:
            sent += dispatch(tenant_id, poster)
        except Exception:
            log.exception("sending webhooks failed for tenant %s", tenant_id)
    return sent


def purge_deliveries(db: Session, now: datetime) -> int:
    result = db.execute(
        delete(WebhookDelivery).where(WebhookDelivery.created_at < now - KEEP_DELIVERIES)
    )
    return result.rowcount


def send_ping(poster: WebhookPoster, hook: Webhook) -> int:
    """A test message, sent right away (the dashboard's test button)."""
    return post_message(
        poster,
        hook.url,
        decrypt_secret(hook.secret_encrypted),
        f"ping:{uuid.uuid4().hex}",
        "ping",
        {"webhook_id": str(hook.id), "name": hook.name},
    )
