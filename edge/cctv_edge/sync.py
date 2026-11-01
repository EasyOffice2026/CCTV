"""Sends the outbox to the cloud: events (media first, then the events in batches),
attendance sightings and count buckets."""

import logging
from uuid import UUID

from pydantic import ValidationError

from cctv_contracts.enums import MediaKind
from cctv_contracts.v1 import (
    MAX_BUCKETS_PER_BATCH,
    MAX_EVENTS_PER_BATCH,
    MAX_HEATMAPS_PER_BATCH,
    MAX_SIGHTINGS_PER_BATCH,
    BatchResult,
    CountBatch,
    EventBatch,
    EventIn,
    HeatmapBatch,
    SightingBatch,
    UploadUrlRequest,
)
from cctv_edge.cloud import CloudClient, CloudError
from cctv_edge.outbox import MediaFile, Outbox, PendingEvent

log = logging.getLogger(__name__)

CONTENT_TYPES = {MediaKind.SNAPSHOT: "image/jpeg", MediaKind.CLIP: "video/mp4"}


class _UnsendableError(Exception):
    """This event can never be accepted; drop it instead of retrying forever."""


class Syncer:
    def __init__(self, outbox: Outbox, client: CloudClient) -> None:
        self.outbox = outbox
        self.client = client

    def sync_once(self, now: float) -> int:
        """Send due events. Returns how many the cloud accepted. Stops at the first sign
        the cloud is unreachable; those events are retried later with backoff."""
        due = self.outbox.due_events(now, MAX_EVENTS_PER_BATCH)
        ready: list[tuple[PendingEvent, EventIn]] = []
        for index, event in enumerate(due):
            try:
                ready.append((event, self._prepare(event)))
            except _UnsendableError as exc:
                log.error("dropping event %s: %s", event.event_id, exc)
                self.outbox.mark_dropped(event.event_id, str(exc))
            except CloudError as exc:
                log.warning("media upload failed: %s", exc)
                self.outbox.mark_retry([e.event_id for e in due[index:]], str(exc), now)
                break
        if not ready:
            return 0

        try:
            response = self.client.send_events(EventBatch(events=[e for _, e in ready]))
        except CloudError as exc:
            log.warning("sending %d events failed: %s", len(ready), exc)
            self.outbox.mark_retry([p.event_id for p, _ in ready], str(exc), now)
            return 0

        revisions: dict[UUID, int] = {p.event_id: p.revision for p, _ in ready}
        accepted = 0
        for result in response.results:
            if result.event_id not in revisions:
                continue
            if result.accepted:
                self.outbox.mark_sent(result.event_id, revisions.pop(result.event_id), now)
                accepted += 1
            else:
                revisions.pop(result.event_id)
                log.error("cloud rejected event %s: %s", result.event_id, result.error)
                self.outbox.mark_dropped(result.event_id, f"rejected: {result.error}")
        if revisions:  # missing from the response: try again later
            self.outbox.mark_retry(revisions, "no result in response", now)
        return accepted

    def sync_sightings(self, now: float) -> int:
        """Send waiting attendance sightings; returns how many the cloud accepted."""
        due = self.outbox.due_sightings(now, MAX_SIGHTINGS_PER_BATCH)
        if not due:
            return 0
        ids = [i for i, _ in due]
        try:
            result = self.client.send_sightings(SightingBatch(sightings=[s for _, s in due]))
        except CloudError as exc:
            if self._retry(exc):
                log.warning("sending %d sightings failed: %s", len(due), exc)
                self.outbox.retry_sightings(ids, now)
            else:
                log.error("cloud refused %d sightings; dropped: %s", len(due), exc)
                self.outbox.delete_sightings(ids)
            return 0
        self._log_rejected("sighting", result)
        # Rejected ones (staff no longer active or consenting) would be rejected again.
        self.outbox.delete_sightings(ids)
        return result.accepted

    def sync_counts(self, now: float) -> int:
        """Send finished count buckets; returns how many the cloud accepted."""
        due = self.outbox.due_counts(now, MAX_BUCKETS_PER_BATCH)
        if not due:
            return 0
        keys = [k for k, _ in due]
        try:
            result = self.client.send_counts(CountBatch(buckets=[b for _, b in due]))
        except CloudError as exc:
            if self._retry(exc):
                log.warning("sending %d count buckets failed: %s", len(due), exc)
                self.outbox.retry_counts(keys, now)
                return 0
            log.error("cloud refused %d count buckets; dropped: %s", len(due), exc)
            result = None
        if result is not None:
            self._log_rejected("count bucket", result)
        self.outbox.mark_counts_sent(keys)  # rejected ones are not resent either
        return result.accepted if result else 0

    def sync_heatmaps(self, now: float) -> int:
        """Send finished heatmap buckets; returns how many the cloud accepted."""
        due = self.outbox.due_heatmaps(now, MAX_HEATMAPS_PER_BATCH)
        if not due:
            return 0
        keys = [k for k, _ in due]
        try:
            result = self.client.send_heatmaps(HeatmapBatch(buckets=[b for _, b in due]))
        except CloudError as exc:
            if self._retry(exc):
                log.warning("sending %d heatmap buckets failed: %s", len(due), exc)
                self.outbox.retry_heatmaps(keys, now)
                return 0
            log.error("cloud refused %d heatmap buckets; dropped: %s", len(due), exc)
            result = None
        if result is not None:
            self._log_rejected("heatmap bucket", result)
        self.outbox.mark_heatmaps_sent(keys)
        return result.accepted if result else 0

    @staticmethod
    def _retry(exc: CloudError) -> bool:
        # 404: a cloud that doesn't have the endpoint yet; keep the data until it does.
        return exc.retryable or exc.status == 404

    @staticmethod
    def _log_rejected(kind: str, result: BatchResult) -> None:
        for item in result.rejected:
            log.warning("cloud rejected %s #%d: %s", kind, item.index, item.error)

    def _prepare(self, event: PendingEvent) -> EventIn:
        keys = {m.kind: m.media_key or self._upload(event.event_id, m) for m in event.media}
        if MediaKind.SNAPSHOT not in keys:
            raise _UnsendableError("no snapshot")
        try:
            return EventIn.model_validate(
                {
                    **event.payload,
                    "snapshot_key": keys[MediaKind.SNAPSHOT],
                    "clip_key": keys.get(MediaKind.CLIP),
                }
            )
        except ValidationError as exc:
            raise _UnsendableError(f"invalid event: {exc}") from exc

    def _upload(self, event_id: UUID, media: MediaFile) -> str:
        if not media.on_disk or not media.path.exists():
            raise _UnsendableError(f"{media.kind.value} file is missing")
        try:
            request = UploadUrlRequest(
                event_id=event_id,
                kind=media.kind,
                content_type=CONTENT_TYPES[media.kind],
                size_bytes=media.size_bytes,
                sha256=media.sha256,
            )
        except ValidationError as exc:
            raise _UnsendableError(f"{media.kind.value} not uploadable: {exc}") from exc
        try:
            target = self.client.upload_url(request)
            self.client.upload(target, media.path, CONTENT_TYPES[media.kind])
        except CloudError as exc:
            if not exc.retryable:
                raise _UnsendableError(f"{media.kind.value} upload refused: {exc}") from exc
            raise
        self.outbox.set_media_key(event_id, media.kind, target.media_key)
        return target.media_key
