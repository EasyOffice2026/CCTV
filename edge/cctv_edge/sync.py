"""Sends outbox events to the cloud: upload media first, then the events in batches."""

import logging
from uuid import UUID

from pydantic import ValidationError

from cctv_contracts.enums import MediaKind
from cctv_contracts.v1 import MAX_EVENTS_PER_BATCH, EventBatch, EventIn, UploadUrlRequest
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
