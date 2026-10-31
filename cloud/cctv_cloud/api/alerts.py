"""Alert preferences, browser push subscriptions and delivery records (plan 6.1-6.5, 6.7).

Each user manages their own alert rules (which events, which channels, quiet hours) and
profile (WhatsApp number and consent, message language, report links). Alert rules need
`events_view`, and only cover branches the user can see; the router checks both again
when an event arrives, so a later change of role or branch access applies at once.
"""

import json
import logging
import uuid
from datetime import datetime, time, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.deps import Context, context, forbidden, not_found
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import AlertChannel, AlertKind, DeliveryStatus, Locale
from cctv_cloud.db.models import AlertDelivery, AlertSubscription, Event, PushSubscription, User
from cctv_cloud.rbac import Permission
from cctv_cloud.services.alerts import (
    SendError,
    Senders,
    get_senders,
    mark_delivery_report,
    recipient_for,
    send_test,
)
from cctv_cloud.services.webpush import PushSender, allowed_endpoint, get_push_sender
from cctv_cloud.services.whatsapp import WhatsApp, get_whatsapp, valid_signature
from cctv_contracts.enums import FeatureKey, Severity

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["alerts"])

Member = Annotated[Context, Depends(context(), scope="function")]
Viewer = Annotated[Context, Depends(context(Permission.EVENTS_VIEW), scope="function")]

MAX_RULES = 20
PHONE_PATTERN = r"^\+[1-9][0-9]{6,14}$"


# --- profile --------------------------------------------------------------------------


class AlertProfileIn(BaseModel):
    phone: str | None = Field(
        default=None, pattern=PHONE_PATTERN, description="WhatsApp number, e.g. +96550000000"
    )
    whatsapp_opt_in: bool = Field(
        default=False, description="The user agrees to receive alerts on WhatsApp"
    )
    locale: Locale = Locale.EN
    report_channels: list[Literal["email", "whatsapp"]] = Field(default_factory=list)

    @field_validator("phone", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.replace(" ", "").replace("-", "")
            return value or None
        return value

    @model_validator(mode="after")
    def _check(self) -> "AlertProfileIn":
        if self.whatsapp_opt_in and not self.phone:
            raise ValueError("a WhatsApp number is needed to receive WhatsApp messages")
        if "whatsapp" in self.report_channels and not self.whatsapp_opt_in:
            raise ValueError("agree to WhatsApp messages to get report links there")
        self.report_channels = sorted(set(self.report_channels))
        return self


class AlertProfileOut(AlertProfileIn):
    email: str
    whatsapp_opt_in_at: datetime | None
    whatsapp_configured: bool = Field(description="False: WhatsApp messages go to the log")
    push_public_key: str | None = Field(
        description="VAPID key for PushManager.subscribe; null when Web Push isn't set up"
    )


def _profile(user: User, whatsapp: WhatsApp, push: PushSender) -> AlertProfileOut:
    return AlertProfileOut(
        phone=user.phone,
        whatsapp_opt_in=user.whatsapp_opt_in_at is not None,
        locale=Locale(user.locale),
        report_channels=sorted(user.report_channels),
        email=user.email,
        whatsapp_opt_in_at=user.whatsapp_opt_in_at,
        whatsapp_configured=whatsapp.configured,
        push_public_key=push.public_key,
    )


def _me(ctx: Context) -> User:
    if ctx.principal.user_id is None:
        raise forbidden("API keys have no alert settings")
    return ctx.db.get_one(User, ctx.principal.user_id)


@router.get("/me/alert-profile", response_model=AlertProfileOut)
def get_alert_profile(
    ctx: Member,
    whatsapp: Annotated[WhatsApp, Depends(get_whatsapp)],
    push: Annotated[PushSender, Depends(get_push_sender)],
) -> AlertProfileOut:
    return _profile(_me(ctx), whatsapp, push)


@router.put("/me/alert-profile", response_model=AlertProfileOut)
def update_alert_profile(
    body: AlertProfileIn,
    ctx: Member,
    whatsapp: Annotated[WhatsApp, Depends(get_whatsapp)],
    push: Annotated[PushSender, Depends(get_push_sender)],
) -> AlertProfileOut:
    user = _me(ctx)
    if body.report_channels and not ctx.principal.can(Permission.REPORTS_VIEW):
        raise forbidden("your role doesn't receive reports")
    if not body.whatsapp_opt_in:
        user.whatsapp_opt_in_at = None
    elif user.whatsapp_opt_in_at is None or body.phone != user.phone:
        user.whatsapp_opt_in_at = datetime.now(timezone.utc)  # consent for this number
    user.phone = body.phone
    user.locale = body.locale.value
    user.report_channels = list(body.report_channels)
    return _profile(user, whatsapp, push)


# --- rules ----------------------------------------------------------------------------


class AlertRuleIn(BaseModel):
    feature_key: FeatureKey | None = Field(default=None, description="null: every feature")
    branch_id: uuid.UUID | None = Field(default=None, description="null: every branch")
    min_severity: Severity = Severity.WARNING
    channels: list[AlertChannel] = Field(min_length=1)
    quiet_from: time | None = Field(default=None, description="Branch time; only critical alerts")
    quiet_until: time | None = None
    notify_resolved: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "AlertRuleIn":
        if (self.quiet_from is None) != (self.quiet_until is None):
            raise ValueError("set both ends of the quiet hours, or neither")
        if self.quiet_from is not None and self.quiet_from == self.quiet_until:
            raise ValueError("quiet hours must not start and end at the same time")
        self.channels = sorted(set(self.channels), key=lambda c: c.value)
        return self


class AlertRuleOut(AlertRuleIn):
    id: uuid.UUID


def _rule_out(rule: AlertSubscription) -> AlertRuleOut:
    return AlertRuleOut(
        id=rule.id,
        feature_key=FeatureKey(rule.feature_key) if rule.feature_key else None,
        branch_id=rule.branch_id,
        min_severity=Severity(rule.min_severity),
        channels=[AlertChannel(c) for c in rule.channels],
        quiet_from=rule.quiet_from,
        quiet_until=rule.quiet_until,
        notify_resolved=rule.notify_resolved,
        enabled=rule.enabled,
    )


def _check_branch(ctx: Context, branch_id: uuid.UUID | None) -> None:
    if branch_id is not None and not ctx.principal.can_access_branch(branch_id):
        raise not_found("branch not found")


def _own_rule(ctx: Context, rule_id: uuid.UUID) -> AlertSubscription:
    rule = ctx.db.get(AlertSubscription, rule_id)
    if rule is None or rule.user_id != ctx.principal.user_id:
        raise not_found("alert rule not found")
    return rule


def _apply(rule: AlertSubscription, body: AlertRuleIn) -> None:
    rule.feature_key = body.feature_key.value if body.feature_key else None
    rule.branch_id = body.branch_id
    rule.min_severity = body.min_severity.value
    rule.channels = [c.value for c in body.channels]
    rule.quiet_from = body.quiet_from
    rule.quiet_until = body.quiet_until
    rule.notify_resolved = body.notify_resolved
    rule.enabled = body.enabled


@router.get("/me/alert-rules", response_model=list[AlertRuleOut])
def list_alert_rules(ctx: Viewer) -> list[AlertRuleOut]:
    rules = ctx.db.scalars(
        select(AlertSubscription)
        .where(AlertSubscription.user_id == ctx.principal.user_id)
        .order_by(AlertSubscription.id)
    )
    return [_rule_out(r) for r in rules]


@router.post("/me/alert-rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
def create_alert_rule(body: AlertRuleIn, ctx: Viewer) -> AlertRuleOut:
    _me(ctx)
    _check_branch(ctx, body.branch_id)
    count = ctx.db.scalar(
        select(func.count()).where(AlertSubscription.user_id == ctx.principal.user_id)
    )
    if count >= MAX_RULES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"at most {MAX_RULES} alert rules")
    rule = AlertSubscription(tenant_id=ctx.principal.tenant_id, user_id=ctx.principal.user_id)
    _apply(rule, body)
    ctx.db.add(rule)
    ctx.db.flush()
    return _rule_out(rule)


@router.put("/me/alert-rules/{rule_id}", response_model=AlertRuleOut)
def update_alert_rule(rule_id: uuid.UUID, body: AlertRuleIn, ctx: Viewer) -> AlertRuleOut:
    rule = _own_rule(ctx, rule_id)
    _check_branch(ctx, body.branch_id)
    _apply(rule, body)
    return _rule_out(rule)


@router.delete("/me/alert-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_rule(rule_id: uuid.UUID, ctx: Viewer) -> None:
    ctx.db.delete(_own_rule(ctx, rule_id))


# --- browser push ---------------------------------------------------------------------


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=80, max_length=200)
    auth: str = Field(min_length=16, max_length=100)


class PushSubscriptionIn(BaseModel):
    """`PushSubscription.toJSON()` from the browser."""

    endpoint: str = Field(max_length=1000)
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def _known_service(cls, value: str) -> str:
        if not allowed_endpoint(value):
            raise ValueError("not a known browser push service")
        return value


@router.put("/me/push-subscriptions", status_code=status.HTTP_204_NO_CONTENT)
def save_push_subscription(body: PushSubscriptionIn, ctx: Viewer, request: Request) -> None:
    """Register this browser for notifications (again: the keys may have changed)."""
    _me(ctx)
    existing = ctx.db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    if existing is None:
        existing = PushSubscription(tenant_id=ctx.principal.tenant_id, endpoint=body.endpoint)
        ctx.db.add(existing)
    existing.user_id = ctx.principal.user_id  # the browser now belongs to whoever signed in
    existing.p256dh = body.keys.p256dh
    existing.auth = body.keys.auth
    existing.user_agent = (request.headers.get("user-agent") or "")[:500] or None
    try:
        with ctx.db.begin_nested():
            ctx.db.flush()
    except IntegrityError as exc:  # registered in another tenant (hidden by RLS)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this browser receives another account's alerts"
        ) from exc


@router.delete("/me/push-subscriptions", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_subscription(ctx: Member, endpoint: Annotated[str, Query(max_length=1000)]) -> None:
    ctx.db.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == ctx.principal.user_id,
        )
    )


# --- test message ---------------------------------------------------------------------


class AlertTest(BaseModel):
    channel: AlertChannel


@router.post("/me/alert-test", status_code=status.HTTP_204_NO_CONTENT)
def test_alert(
    body: AlertTest, ctx: Viewer, senders: Annotated[Senders, Depends(get_senders)]
) -> None:
    """Send a sample message on one channel to the caller (to check the setup)."""
    _me(ctx)
    recipient = recipient_for(ctx.db, ctx.principal.user_id)
    if body.channel is AlertChannel.WEB and not recipient.push:
        # Still useful: the open dashboard shows the toast.
        log.info("test alert for user %s: no browser registered for push", recipient.user_id)
    try:
        send_test(senders, recipient, body.channel.value, branch_id="")
    except SendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# --- deliveries of an event -----------------------------------------------------------


class DeliveryOut(BaseModel):
    user_name: str
    channel: AlertChannel
    kind: AlertKind
    status: DeliveryStatus
    created_at: datetime
    sent_at: datetime | None
    latency_ms: int | None = Field(description="Event start to sent (ms)")
    delivered_at: datetime | None
    attempts: int
    error: str | None


@router.get("/events/{event_id}/alerts", response_model=list[DeliveryOut], tags=["events"])
def event_alerts(event_id: uuid.UUID, ctx: Viewer) -> list[DeliveryOut]:
    """Who was alerted about an event, how, and how fast (plan 6.7)."""
    event = ctx.db.get(Event, event_id)
    if event is None or not ctx.principal.can_access_branch(event.branch_id):
        raise not_found("event not found")
    rows = ctx.db.execute(
        select(AlertDelivery, User.full_name)
        .join(User, User.id == AlertDelivery.user_id)
        .where(AlertDelivery.event_id == event_id)
        .order_by(AlertDelivery.id)
    ).all()
    return [
        DeliveryOut(
            user_name=name,
            channel=AlertChannel(d.channel),
            kind=AlertKind(d.kind),
            status=DeliveryStatus(d.status),
            created_at=d.created_at,
            sent_at=d.sent_at,
            latency_ms=d.latency_ms,
            delivered_at=d.delivered_at,
            attempts=d.attempts,
            error=d.error,
        )
        for d, name in rows
    ]


# --- WhatsApp delivery reports --------------------------------------------------------


@router.get("/webhooks/whatsapp", include_in_schema=False)
def whatsapp_verify(
    mode: Annotated[str, Query(alias="hub.mode")] = "",
    token: Annotated[str, Query(alias="hub.verify_token")] = "",
    challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> PlainTextResponse:
    """Meta checks the webhook URL once when it is configured."""
    expected = get_settings().whatsapp_verify_token.get_secret_value()
    if mode != "subscribe" or not expected or token != expected:
        raise forbidden()
    return PlainTextResponse(challenge)


@router.post("/webhooks/whatsapp", include_in_schema=False)
async def whatsapp_status(request: Request) -> Response:
    """Message status updates (sent, delivered, read, failed), signed with the app secret."""
    body = await request.body()
    if not valid_signature(body, request.headers.get("x-hub-signature-256")):
        raise forbidden("invalid signature")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for item in (change.get("value") or {}).get("statuses") or []:
                try:
                    at = datetime.fromtimestamp(int(item["timestamp"]), timezone.utc)
                    errors = item.get("errors") or [{}]
                    error = errors[0].get("title") or errors[0].get("message")
                    await run_in_threadpool(
                        mark_delivery_report, str(item["id"]), str(item["status"]), at, error
                    )
                except (KeyError, ValueError, TypeError):
                    log.warning("unreadable WhatsApp status: %s", item)
    return Response(status_code=status.HTTP_200_OK)
