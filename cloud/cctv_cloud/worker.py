"""Background jobs (plan 3.8): retention and offline devices.

    python -m cctv_cloud.worker retention   # once
    python -m cctv_cloud.worker devices     # once
    python -m cctv_cloud.worker run         # both, every few minutes, until stopped

Runs as the restricted app role, one tenant at a time. Object-storage lifecycle rules
(expire objects after the longest retention a tenant can choose) are a backstop in case
this job stops running.
"""

import argparse
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, text, update

from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import DeviceStatus
from cctv_cloud.db.models import Camera, CountBucket, EdgeDevice, Event, Tenant
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services.storage import ObjectStorage, get_storage

log = logging.getLogger("cctv_cloud.worker")

BATCH = 500


@dataclass
class RetentionReport:
    media_cleared: int = 0
    objects_deleted: int = 0
    events_deleted: int = 0
    failed_tenants: list[uuid.UUID] = field(default_factory=list)


def tenant_ids() -> list[uuid.UUID]:
    with tenant_session(None) as db:
        return list(db.execute(text("SELECT tenant_id FROM platform_list_tenants()")).scalars())


def apply_retention(
    storage: ObjectStorage, now: datetime | None = None, tenants: list[uuid.UUID] | None = None
) -> RetentionReport:
    """Delete media older than each tenant's media retention, then event records older
    than its event retention (plan §6). Objects are deleted before their keys are cleared,
    so a failure leaves the keys in place and the next run retries."""
    now = now or datetime.now(timezone.utc)
    report = RetentionReport()
    for tenant_id in tenants if tenants is not None else tenant_ids():
        try:
            _retain_tenant(storage, tenant_id, now, report)
        except Exception:
            log.exception("retention failed for tenant %s", tenant_id)
            report.failed_tenants.append(tenant_id)
    return report


def _retain_tenant(
    storage: ObjectStorage, tenant_id: uuid.UUID, now: datetime, report: RetentionReport
) -> None:
    with tenant_session(tenant_id) as db:
        tenant = db.get_one(Tenant, tenant_id)
        media_cutoff = now - timedelta(days=tenant.retention_media_days)
        event_cutoff = now - timedelta(days=tenant.retention_event_days)

    while True:
        with tenant_session(tenant_id) as db:
            rows = db.execute(
                select(Event.id, Event.snapshot_key, Event.clip_key)
                .where(
                    Event.started_at < media_cutoff,
                    or_(Event.snapshot_key.is_not(None), Event.clip_key.is_not(None)),
                )
                .limit(BATCH)
            ).all()
            if not rows:
                break
            keys = [k for r in rows for k in (r.snapshot_key, r.clip_key) if k]
            storage.delete(keys)
            db.execute(
                update(Event)
                .where(Event.id.in_([r.id for r in rows]))
                .values(snapshot_key=None, clip_key=None)
            )
        report.media_cleared += len(rows)
        report.objects_deleted += len(keys)

    with tenant_session(tenant_id) as db:
        result = db.execute(delete(Event).where(Event.started_at < event_cutoff))
        db.execute(delete(CountBucket).where(CountBucket.bucket_start < event_cutoff))
        report.events_deleted += result.rowcount


def mark_offline_devices(now: datetime | None = None) -> int:
    """Devices without a heartbeat for a while go offline, and so do their cameras."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=get_settings().device_offline_seconds)
    count = 0
    for tenant_id in tenant_ids():
        with tenant_session(tenant_id) as db:
            branches = list(
                db.scalars(
                    update(EdgeDevice)
                    .where(
                        EdgeDevice.status == DeviceStatus.ONLINE.value,
                        EdgeDevice.last_seen_at < cutoff,
                    )
                    .values(status=DeviceStatus.OFFLINE.value)
                    .returning(EdgeDevice.branch_id)
                )
            )
            if branches:
                db.execute(
                    update(Camera).where(Camera.branch_id.in_(branches)).values(health="offline")
                )
                log.warning("tenant %s: %d edge devices went offline", tenant_id, len(branches))
            count += len(branches)
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cctv_cloud.worker")
    parser.add_argument("job", choices=["retention", "devices", "run"])
    parser.add_argument("--interval", type=int, default=300, help="seconds between runs (run)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.job == "retention":
        log.info("retention: %s", apply_retention(get_storage()))
    elif args.job == "devices":
        log.info("devices marked offline: %d", mark_offline_devices())
    else:
        last_retention = 0.0
        while True:
            mark_offline_devices()
            if time.monotonic() - last_retention > 3600:
                log.info("retention: %s", apply_retention(get_storage()))
                last_retention = time.monotonic()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
