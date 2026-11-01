"""The till (POS) and the cash drawer (later phase: drawer vs sales).

The till sends its sales with an API key (`POST /v1/pos/sales`), with `synced_until`: the
time up to which it has sent everything. Every minute the worker checks each cash drawer
opening the till is complete for: a sale from BEFORE before it to AFTER after it explains
it (cash first, then the nearest in time; each sale explains one opening). An opening
without one is marked `no_sale` and raised to a warning, for the review list, and alerted
like a new warning ("Cash drawer opened without a sale") to whoever's alert rules ask for
cashier warnings; people already told about that opening aren't told twice.

Openings are only checked up to `synced_until`, so a till that is offline or behind
doesn't make every opening look like one without a sale.
"""

import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from cctv_cloud.db.enums import PosMatch, PosPayment
from cctv_cloud.db.models import Branch, Event, PosSale
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services.alerts import Change, route_event
from cctv_contracts.enums import EventType, Severity

log = logging.getLogger(__name__)

BEFORE = timedelta(seconds=60)  # the sale rung up just before the drawer opens
AFTER = timedelta(seconds=120)  # or after it (change given first, receipt printed later)
MAX_AGE = timedelta(days=7)  # older openings aren't checked any more
_UNIQUE = "uq_pos_sales_branch_id_receipt_id"


def store_sales(db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID, sales: Sequence) -> int:
    """Insert or update sales by receipt; a sale already matched keeps its match."""
    rows: dict[str, dict] = {}  # one row per receipt per INSERT
    for sale in sales:
        rows[sale.receipt_id] = {
            "tenant_id": tenant_id,
            "branch_id": branch_id,
            "receipt_id": sale.receipt_id,
            "sold_at": sale.sold_at,
            "total": sale.total,
            "payment": sale.payment.value,
            "till": sale.till,
        }
    if rows:
        statement = insert(PosSale).values(list(rows.values()))
        db.execute(
            statement.on_conflict_do_update(
                constraint=_UNIQUE,
                set_={c: statement.excluded[c] for c in ("sold_at", "total", "payment", "till")},
            )
        )
    return len(rows)


def _match(
    events: Sequence[Event], sales: Iterable[PosSale], now: datetime
) -> tuple[int, list[Event]]:
    free = list(sales)
    matched = 0
    no_sale: list[Event] = []
    for event in events:
        candidates = [
            s for s in free if event.started_at - BEFORE <= s.sold_at <= event.started_at + AFTER
        ]
        event.pos_checked_at = now
        if candidates:
            sale = min(
                candidates,
                key=lambda s: (
                    s.payment != PosPayment.CASH.value,
                    abs(s.sold_at - event.started_at),
                ),
            )
            free.remove(sale)
            sale.matched_event_id = event.id
            event.pos_match, event.pos_receipt_id = PosMatch.SALE.value, sale.receipt_id
            matched += 1
        else:
            event.pos_match = PosMatch.NO_SALE.value
            if event.severity == Severity.INFO.value:
                event.severity = Severity.WARNING.value
            no_sale.append(event)
    return matched, no_sale


def reconcile_tenant(tenant_id: uuid.UUID, now: datetime | None = None) -> tuple[int, int]:
    """Check the drawer openings the till is complete for. Returns (with a sale, without)."""
    now = now or datetime.now(timezone.utc)
    totals = (0, 0)
    with tenant_session(tenant_id) as db:
        branches = db.execute(
            select(Branch.id, Branch.pos_synced_until).where(Branch.pos_synced_until.is_not(None))
        ).all()
        for branch_id, synced_until in branches:
            events = db.scalars(
                select(Event)
                .where(
                    Event.branch_id == branch_id,
                    Event.event_type == EventType.DRAWER_OPEN.value,
                    Event.pos_checked_at.is_(None),
                    Event.started_at >= now - MAX_AGE,
                    Event.started_at <= synced_until - AFTER,
                )
                .order_by(Event.started_at)
                .limit(1000)
            ).all()
            if not events:
                continue
            sales = db.scalars(
                select(PosSale).where(
                    PosSale.branch_id == branch_id,
                    PosSale.matched_event_id.is_(None),
                    PosSale.sold_at >= events[0].started_at - BEFORE,
                    PosSale.sold_at <= events[-1].started_at + AFTER,
                )
            ).all()
            matched, no_sale = _match(events, sales, now)
            db.flush()
            for event in no_sale:  # queued; the worker sends them in its next round
                route_event(db, event, Change.NEW, now)
            totals = (totals[0] + matched, totals[1] + len(no_sale))
    if totals[1]:
        log.info("tenant %s: %d drawer openings without a sale", tenant_id, totals[1])
    return totals


def reconcile_all(tenant_ids: Iterable[uuid.UUID], now: datetime | None = None) -> int:
    """Worker: every tenant; returns how many openings had no sale."""
    no_sale = 0
    for tenant_id in tenant_ids:
        try:
            no_sale += reconcile_tenant(tenant_id, now)[1]
        except Exception:
            log.exception("checking drawer openings failed for tenant %s", tenant_id)
    return no_sale
