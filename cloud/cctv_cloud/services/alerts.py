"""Alert routing and sending (plan 6.1-6.4, 6.7).

Routing runs when the edge reports an event, in the same transaction: the user's alert
rules (feature, branch, minimum severity, quiet hours) pick who is told on which
channels, and one `alert_deliveries` row is queued per user and channel. Because rows are
unique per event, user, channel and kind, an event that is updated or sent again never
alerts twice; a cooldown also stops repeats for the same camera, zone and event type.
When the event ends, users whose rule asks for it get a "resolved" message.

Sending happens after the commit: right away from the API (a background task), and the
worker retries what is still queued. A sender claims a row by moving its
`next_attempt_at` forward, so two senders never send the same message. `latency_ms` is
the time from the event's start (edge clock) to the message being accepted by the channel.
"""

import logging
import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import AlertChannel, AlertKind, DeliveryStatus, UserRole
from cctv_cloud.db.models import (
    AlertDelivery,
    AlertSubscription,
    Branch,
    Camera,
    Event,
    PushSubscription,
    User,
    UserBranchAccess,
    Zone,
)
from cctv_cloud.db.session import tenant_session
from cctv_cloud.rbac import ALL_BRANCH_ROLES, ROLE_PERMISSIONS, Permission
from cctv_cloud.services import texts
from cctv_cloud.services.mailer import Mailer, get_mailer
from cctv_cloud.services.notify import Publisher, get_publisher
from cctv_cloud.services.webpush import PushGoneError, PushSender, get_push_sender
from cctv_cloud.services.whatsapp import WhatsApp, WhatsAppError, get_whatsapp
from cctv_contracts.enums import Severity

log = logging.getLogger(__name__)

SEVERITY_RANK = {Severity.INFO.value: 0, Severity.WARNING.value: 1, Severity.CRITICAL.value: 2}
# A claimed delivery is retried after this if its sender died mid-way.
CLAIM_SECONDS = 60
# Queued messages older than this are dropped instead of sent late.
EXPIRE_AFTER = timedelta(hours=1)
BATCH = 100


class Change(str, Enum):
    NEW = "new"
    UPDATED = "updated"
    CLOSED = "closed"  # was open, now closed


# --- routing --------------------------------------------------------------------------


def in_quiet_hours(start: time | None, end: time | None, at: time) -> bool:
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= at < end
    return at >= start or at < end  # e.g. 23:00-07:00


def _visible_to(role: str, user_id: uuid.UUID, branch_id: uuid.UUID, access: set) -> bool:
    user_role = UserRole(role)
    if Permission.EVENTS_VIEW not in ROLE_PERMISSIONS[user_role]:
        return False
    return user_role in ALL_BRANCH_ROLES or (user_id, branch_id) in access


def _wanted_channels(
    db: Session, event: Event, now: datetime, *, resolved_only: bool = False
) -> dict[uuid.UUID, set[str]]:
    """Channels per user whose enabled rules match the event (and who may see it)."""
    rows = db.execute(
        select(AlertSubscription, User)
        .join(User, User.id == AlertSubscription.user_id)
        .where(
            AlertSubscription.enabled,
            User.is_active,
            or_(
                AlertSubscription.feature_key.is_(None),
                AlertSubscription.feature_key == event.feature_key,
            ),
            or_(
                AlertSubscription.branch_id.is_(None),
                AlertSubscription.branch_id == event.branch_id,
            ),
        )
    ).all()
    if not rows:
        return {}
    access = set(
        db.execute(
            select(UserBranchAccess.user_id, UserBranchAccess.branch_id).where(
                UserBranchAccess.user_id.in_({user.id for _, user in rows}),
                UserBranchAccess.branch_id == event.branch_id,
            )
        ).all()
    )
    branch = db.get_one(Branch, event.branch_id)
    local_time = now.astimezone(ZoneInfo(branch.timezone)).time()
    # Shadow mode (plan 9.4): only the support team's reviewers are told.
    shadow = branch.shadow_until is not None and branch.shadow_until > now
    critical = event.severity == Severity.CRITICAL.value

    wanted: dict[uuid.UUID, set[str]] = {}
    for rule, user in rows:
        if resolved_only and not rule.notify_resolved:
            continue
        if shadow and not user.shadow_reviewer:
            continue
        if SEVERITY_RANK[event.severity] < SEVERITY_RANK[rule.min_severity]:
            continue
        if not critical and in_quiet_hours(rule.quiet_from, rule.quiet_until, local_time):
            continue
        if not _visible_to(user.role, user.id, event.branch_id, access):
            continue
        channels = set(rule.channels)
        if not (user.phone and user.whatsapp_opt_in_at):
            channels.discard(AlertChannel.WHATSAPP.value)
        wanted.setdefault(user.id, set()).update(channels)
    return {user_id: channels for user_id, channels in wanted.items() if channels}


def _queue(
    db: Session, event: Event, kind: AlertKind, targets: Iterable[tuple[uuid.UUID, str]], now
) -> list[int]:
    values = [
        {
            "tenant_id": event.tenant_id,
            "event_id": event.id,
            "user_id": user_id,
            "channel": channel,
            "kind": kind.value,
            "created_at": now,
            "next_attempt_at": now,
        }
        for user_id, channel in targets
    ]
    if not values:
        return []
    return list(
        db.scalars(
            insert(AlertDelivery)
            .values(values)
            .on_conflict_do_nothing(constraint="uq_alert_deliveries_event_id_user_id_channel_kind")
            .returning(AlertDelivery.id)
        )
    )


def _cooling_down(db: Session, event: Event, now: datetime) -> set[tuple[uuid.UUID, str]]:
    """(user, channel) pairs alerted recently for the same camera, zone and event type."""
    since = now - timedelta(seconds=get_settings().alert_cooldown_seconds)
    return set(
        db.execute(
            select(AlertDelivery.user_id, AlertDelivery.channel)
            .join(Event, Event.id == AlertDelivery.event_id)
            .where(
                AlertDelivery.kind == AlertKind.ALERT.value,
                AlertDelivery.status != DeliveryStatus.FAILED.value,
                AlertDelivery.created_at > since,
                AlertDelivery.event_id != event.id,
                Event.camera_id == event.camera_id,
                Event.zone_id.is_not_distinct_from(event.zone_id),
                Event.event_type == event.event_type,
            )
        ).all()
    )


def route_event(db: Session, event: Event, change: Change, now: datetime) -> list[int]:
    """Queue the deliveries an event change calls for; returns their ids."""
    if change is Change.NEW:
        max_age = timedelta(minutes=get_settings().alert_max_age_minutes)
        if now - event.started_at > max_age:
            return []  # arrived late (e.g. after an outage): in the feed, not an alert
        wanted = _wanted_channels(db, event, now)
        cooling = _cooling_down(db, event, now) if wanted else set()
        targets = [
            (user_id, channel)
            for user_id, channels in wanted.items()
            for channel in sorted(channels)
            if (user_id, channel) not in cooling
        ]
        return _queue(db, event, AlertKind.ALERT, targets, now)
    if change is Change.CLOSED:
        alerted = set(
            db.execute(
                select(AlertDelivery.user_id, AlertDelivery.channel).where(
                    AlertDelivery.event_id == event.id,
                    AlertDelivery.kind == AlertKind.ALERT.value,
                    AlertDelivery.status != DeliveryStatus.FAILED.value,
                )
            ).all()
        )
        if not alerted:
            return []
        wanted = _wanted_channels(db, event, now, resolved_only=True)
        targets = sorted(
            (user_id, channel)
            for user_id, channels in wanted.items()
            for channel in channels
            if (user_id, channel) in alerted
        )
        return _queue(db, event, AlertKind.RESOLVED, targets, now)
    return []


# --- messages -------------------------------------------------------------------------


@dataclass
class Message:
    """One alert, worded for its recipient."""

    title: str
    body: str
    url: str
    event_id: str
    kind: str
    severity: str
    branch_id: str
    # WhatsApp template parameters: what, where (branch), camera, when.
    params: list[str] = field(default_factory=list)


def event_url(event_id: object) -> str:
    return f"{get_settings().web_url.rstrip('/')}/events/?event={event_id}"


def build_message(
    locale: str,
    kind: str,
    event: Event,
    branch: Branch,
    camera_name: str,
    zone_name: str | None,
) -> Message:
    what = texts.event_label(locale, event.event_type)
    if kind == AlertKind.RESOLVED.value:
        title = f"{texts.t(locale, 'alert.resolved')}: {what}"
        at = event.ended_at or event.started_at
    else:
        title = f"{texts.t(locale, f'severity.{event.severity}')}: {what}"
        at = event.started_at
    local = at.astimezone(ZoneInfo(branch.timezone))
    where = camera_name + (f" · {zone_name}" if zone_name else "")
    return Message(
        title=title,
        body=f"{branch.name} · {where} · {local:%H:%M}",
        url=event_url(event.id),
        event_id=str(event.id),
        kind=kind,
        severity=event.severity,
        branch_id=str(event.branch_id),
        params=[what, branch.name, where, f"{local:%Y-%m-%d %H:%M}"],
    )


# --- sending --------------------------------------------------------------------------


@dataclass
class Senders:
    publisher: Publisher
    push: PushSender
    whatsapp: WhatsApp
    mailer: Mailer


def get_senders(
    publisher: Annotated[Publisher, Depends(get_publisher)],
    push: Annotated[PushSender, Depends(get_push_sender)],
    whatsapp: Annotated[WhatsApp, Depends(get_whatsapp)],
    mailer: Annotated[Mailer, Depends(get_mailer)],
) -> Senders:
    return Senders(publisher, push, whatsapp, mailer)


def default_senders() -> Senders:
    return Senders(get_publisher(), get_push_sender(), get_whatsapp(), get_mailer())


class SendError(Exception):
    def __init__(self, message: str, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass
class Recipient:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    phone: str | None
    whatsapp_ok: bool
    locale: str
    push: list[tuple[uuid.UUID, str, str, str]]  # id, endpoint, p256dh, auth


@dataclass
class SendResult:
    provider_message_id: str | None = None
    note: str | None = None  # a partial failure (e.g. one browser) on a sent message
    gone_push: list[uuid.UUID] = field(default_factory=list)
    push_ok: list[uuid.UUID] = field(default_factory=list)


def send(senders: Senders, channel: str, to: Recipient, message: Message) -> SendResult:
    """Send one message; raises SendError."""
    result = SendResult()
    if channel == AlertChannel.WEB.value:
        # In-app toast for open dashboards (best effort), then Web Push to the user's browsers.
        senders.publisher.publish(
            f"events:{to.tenant_id}",
            {
                "type": "alert",
                "user_id": str(to.user_id),
                "event_id": message.event_id,
                "kind": message.kind,
                "severity": message.severity,
                "branch_id": message.branch_id,
                "title": message.title,
                "body": message.body,
                "url": message.url,
            },
        )
        errors = []
        payload = {
            "title": message.title,
            "body": message.body,
            "url": message.url,
            "tag": f"{message.event_id}:{message.kind}",
        }
        for push_id, endpoint, p256dh, auth in to.push:
            try:
                senders.push.send(endpoint, p256dh, auth, payload)
                result.push_ok.append(push_id)
            except PushGoneError:
                result.gone_push.append(push_id)
            except Exception as exc:  # one broken browser shouldn't fail the alert
                errors.append(f"push: {exc}"[:200])
        result.note = "; ".join(errors) or None
        return result
    if channel == AlertChannel.WHATSAPP.value:
        if not (to.phone and to.whatsapp_ok):
            raise SendError("no WhatsApp number or consent", retry=False)
        templates = get_settings().whatsapp_templates
        try:
            result.provider_message_id = senders.whatsapp.send_template(
                to.phone,
                templates[message.kind],
                texts.lang(to.locale),
                message.params,
                message.event_id,
            )
        except WhatsAppError as exc:
            raise SendError(str(exc), retry=exc.retry) from exc
        return result
    if channel == AlertChannel.EMAIL.value:
        open_text = texts.t(to.locale, "alert.open")
        try:
            senders.mailer.send(
                to.email, message.title, f"{message.body}\n\n{open_text}: {message.url}\n"
            )
        except Exception as exc:
            raise SendError(f"email: {exc}") from exc
        return result
    raise SendError(f"unknown channel {channel}", retry=False)


def _recipient(db: Session, user: User) -> Recipient:
    push = db.execute(
        select(
            PushSubscription.id,
            PushSubscription.endpoint,
            PushSubscription.p256dh,
            PushSubscription.auth,
        ).where(PushSubscription.user_id == user.id)
    ).all()
    return Recipient(
        tenant_id=user.tenant_id,
        user_id=user.id,
        email=user.email,
        phone=user.phone,
        whatsapp_ok=user.whatsapp_opt_in_at is not None,
        locale=user.locale,
        push=list(push),
    )


@dataclass
class _Job:
    delivery_id: int
    channel: str
    attempts: int
    recipient: Recipient
    message: Message
    started_at: datetime


def _claim(db: Session, now: datetime, ids: list[int] | None) -> list[int]:
    due = (
        select(AlertDelivery.id)
        .where(
            AlertDelivery.status == DeliveryStatus.QUEUED.value,
            AlertDelivery.next_attempt_at <= now,
        )
        .order_by(AlertDelivery.next_attempt_at)
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    )
    if ids is not None:
        due = due.where(AlertDelivery.id.in_(ids))
    return list(
        db.scalars(
            update(AlertDelivery)
            .where(AlertDelivery.id.in_(due.scalar_subquery()))
            .values(
                attempts=AlertDelivery.attempts + 1,
                next_attempt_at=now + timedelta(seconds=CLAIM_SECONDS),
            )
            .returning(AlertDelivery.id)
        )
    )


def _prepare(db: Session, ids: list[int], now: datetime) -> list[_Job]:
    jobs = []
    rows = db.execute(
        select(AlertDelivery, Event, User, Branch, Camera.name, Zone.name)
        .join(Event, Event.id == AlertDelivery.event_id)
        .join(User, User.id == AlertDelivery.user_id)
        .join(Branch, Branch.id == Event.branch_id)
        .join(Camera, Camera.id == Event.camera_id)
        .outerjoin(Zone, Zone.id == Event.zone_id)
        .where(AlertDelivery.id.in_(ids))
    ).all()
    for delivery, event, user, branch, camera_name, zone_name in rows:
        if delivery.created_at < now - EXPIRE_AFTER or not user.is_active:
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error = "expired" if user.is_active else "user deactivated"
            continue
        jobs.append(
            _Job(
                delivery_id=delivery.id,
                channel=delivery.channel,
                attempts=delivery.attempts,
                recipient=_recipient(db, user),
                message=build_message(
                    user.locale, delivery.kind, event, branch, camera_name, zone_name
                ),
                started_at=(
                    event.ended_at or event.started_at
                    if delivery.kind == AlertKind.RESOLVED.value
                    else event.started_at
                ),
            )
        )
    return jobs


def _whatsapp_sent_last_hour(db: Session, user_id: uuid.UUID, now: datetime) -> int:
    return db.scalar(
        select(func.count()).where(
            AlertDelivery.user_id == user_id,
            AlertDelivery.channel == AlertChannel.WHATSAPP.value,
            AlertDelivery.status.in_([DeliveryStatus.SENT.value, DeliveryStatus.DELIVERED.value]),
            AlertDelivery.sent_at > now - timedelta(hours=1),
        )
    )


def dispatch(
    tenant_id: uuid.UUID,
    senders: Senders,
    ids: list[int] | None = None,
    now: datetime | None = None,
) -> int:
    """Send this tenant's due deliveries (only `ids`, if given). Returns how many were sent."""
    settings = get_settings()
    sent = 0
    while True:
        claim_time = now or datetime.now(timezone.utc)
        with tenant_session(tenant_id) as db:
            claimed = _claim(db, claim_time, ids)
            jobs = _prepare(db, claimed, claim_time) if claimed else []
        if not claimed:
            return sent
        for job in jobs:
            outcome: dict[str, Any] = {}
            with tenant_session(tenant_id) as db:
                limited = (
                    job.channel == AlertChannel.WHATSAPP.value
                    and _whatsapp_sent_last_hour(db, job.recipient.user_id, claim_time)
                    >= settings.whatsapp_max_per_hour
                )
            if limited:
                outcome = {"status": DeliveryStatus.FAILED.value, "error": "rate limit reached"}
                result = SendResult()
            else:
                try:
                    result = send(senders, job.channel, job.recipient, job.message)
                except SendError as exc:
                    result = SendResult()
                    if exc.retry and job.attempts < settings.alert_max_attempts:
                        backoff = min(5 * 2 ** (job.attempts - 1), 300)
                        outcome = {
                            "error": str(exc)[:1000],
                            "next_attempt_at": datetime.now(timezone.utc)
                            + timedelta(seconds=backoff),
                        }
                    else:
                        outcome = {"status": DeliveryStatus.FAILED.value, "error": str(exc)[:1000]}
                else:
                    sent_at = datetime.now(timezone.utc)
                    outcome = {
                        "status": DeliveryStatus.SENT.value,
                        "sent_at": sent_at,
                        "latency_ms": max(
                            0, int((sent_at - job.started_at).total_seconds() * 1000)
                        ),
                        "provider_message_id": result.provider_message_id,
                        "error": result.note,
                    }
                    sent += 1
            with tenant_session(tenant_id) as db:
                db.execute(
                    update(AlertDelivery)
                    .where(AlertDelivery.id == job.delivery_id)
                    .values(**outcome)
                )
                if result.gone_push:
                    db.execute(
                        delete(PushSubscription).where(PushSubscription.id.in_(result.gone_push))
                    )
                if result.push_ok:
                    db.execute(
                        update(PushSubscription)
                        .where(PushSubscription.id.in_(result.push_ok))
                        .values(last_success_at=func.now())
                    )
        if ids is not None:
            return sent


def due_tenants() -> list[uuid.UUID]:
    with tenant_session(None) as db:
        return list(db.scalars(text("SELECT tenant_id FROM alert_due_tenants()")))


def dispatch_all(senders: Senders) -> int:
    """Worker: send every tenant's due deliveries."""
    sent = 0
    for tenant_id in due_tenants():
        try:
            sent += dispatch(tenant_id, senders)
        except Exception:
            log.exception("sending alerts failed for tenant %s", tenant_id)
    return sent


def mark_delivery_report(
    provider_message_id: str, status: str, at: datetime, error: str | None
) -> bool:
    """A WhatsApp status update: delivered (or read), or failed after it was accepted."""
    with tenant_session(None) as db:
        found = db.execute(
            text("SELECT delivery_id, tenant_id FROM alert_find_delivery(:id)"),
            {"id": provider_message_id},
        ).first()
    if found is None:
        return False
    delivery_id, tenant_id = found
    with tenant_session(tenant_id) as db:
        delivery = db.get(AlertDelivery, delivery_id)
        if delivery is None:
            return False
        if status in ("delivered", "read") and delivery.delivered_at is None:
            delivery.status = DeliveryStatus.DELIVERED.value
            delivery.delivered_at = at
        elif status == "failed":
            delivery.status = DeliveryStatus.FAILED.value
            delivery.error = (error or "failed")[:1000]
    return True


def send_test(senders: Senders, recipient: Recipient, channel: str, branch_id: str) -> None:
    """A sample message on one channel, for the preferences page; raises SendError."""
    locale = recipient.locale
    title = texts.t(locale, "alert.test_title")
    message = Message(
        title=title,
        body=texts.t(locale, "alert.test_body"),
        url=f"{get_settings().web_url.rstrip('/')}/alerts/",
        event_id="test",
        kind=AlertKind.ALERT.value,
        severity=Severity.INFO.value,
        branch_id=branch_id,
        params=[title, "-", "-", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
    )
    send(senders, channel, recipient, message)


def recipient_for(db: Session, user_id: uuid.UUID) -> Recipient:
    return _recipient(db, db.get_one(User, user_id))


def latency_stats(latencies: list[int]) -> dict[str, int | float | None]:
    """p50/p95 and the share within the 10 s target, for reports."""
    if not latencies:
        return {"p50_ms": None, "p95_ms": None, "within_10s_pct": None}
    ordered = sorted(latencies)

    def pct(p: float) -> int:  # nearest rank
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    within = sum(1 for v in ordered if v <= 10_000)
    return {
        "p50_ms": pct(0.5),
        "p95_ms": pct(0.95),
        "within_10s_pct": round(100 * within / len(ordered), 1),
    }
