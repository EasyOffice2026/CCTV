"""Local outbox: every event and its media are written here before anything is sent.

The sync loop sends from the outbox and only marks an event sent once the cloud accepts
it, so events survive internet outages and restarts. Each change to an event (open ->
closed) bumps its revision; the cloud upserts by `event_id`, so resending is harmless.
"""

import contextlib
import hashlib
import json
import random
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from cctv_contracts.enums import EventStatus, MediaKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    payload         TEXT NOT NULL,           -- EventIn fields except the media keys
    revision        INTEGER NOT NULL DEFAULT 1,
    sent_revision   INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    ended_at        REAL,
    clip_due_at     REAL,                    -- the clip is cut once the post-roll is recorded
    dropped         INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    event_id   TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    media_key  TEXT,
    on_disk    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, kind)
);
CREATE INDEX IF NOT EXISTS events_unsent ON events (next_attempt_at)
    WHERE sent_revision < revision AND dropped = 0;
CREATE INDEX IF NOT EXISTS events_clip_due ON events (clip_due_at)
    WHERE clip_due_at IS NOT NULL;
"""

MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class MediaFile:
    kind: MediaKind
    path: Path
    size_bytes: int
    sha256: str
    media_key: str | None
    on_disk: bool


@dataclass(frozen=True)
class PendingEvent:
    event_id: UUID
    revision: int
    payload: dict[str, Any]
    media: list[MediaFile]


@dataclass(frozen=True)
class ClipJob:
    event_id: UUID
    camera_id: UUID
    started_at: float
    ended_at: float
    snapshot: Path | None


def _file_info(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Outbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _tx(self, statements: Iterable[tuple[str, tuple]]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    self._db.execute(sql, params)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    @staticmethod
    def _media_insert(event_id: UUID, kind: MediaKind, path: Path) -> tuple[str, tuple]:
        size, sha = _file_info(path)
        return (
            "INSERT OR REPLACE INTO media (event_id, kind, path, size_bytes, sha256)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(event_id), kind.value, str(path), size, sha),
        )

    # --- writing events -------------------------------------------------------------

    def open_event(self, payload: dict[str, Any], snapshot: Path, now: float) -> None:
        event_id = UUID(payload["event_id"])
        self._tx(
            [
                (
                    "INSERT INTO events (event_id, payload, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (str(event_id), json.dumps(payload), now, now),
                ),
                self._media_insert(event_id, MediaKind.SNAPSHOT, snapshot),
            ]
        )

    def end_event(self, event_id: UUID, ended_at: float, clip_due_at: float, now: float) -> None:
        self._tx(
            [
                (
                    "UPDATE events SET ended_at = ?, clip_due_at = ?, updated_at = ?"
                    " WHERE event_id = ?",
                    (ended_at, clip_due_at, now, str(event_id)),
                )
            ]
        )

    def clips_due(self, now: float) -> list[ClipJob]:
        rows = self._query(
            "SELECT e.event_id, e.payload, e.ended_at, m.path AS snapshot FROM events e"
            " LEFT JOIN media m ON m.event_id = e.event_id AND m.kind = 'snapshot'"
            " AND m.on_disk = 1"
            " WHERE e.clip_due_at IS NOT NULL AND e.clip_due_at <= ? AND e.dropped = 0"
            " ORDER BY e.clip_due_at",
            (now,),
        )
        jobs = []
        for row in rows:
            payload = json.loads(row["payload"])
            jobs.append(
                ClipJob(
                    UUID(row["event_id"]),
                    UUID(payload["camera_id"]),
                    datetime.fromisoformat(payload["started_at"]).timestamp(),
                    row["ended_at"],
                    Path(row["snapshot"]) if row["snapshot"] else None,
                )
            )
        return jobs

    def close_event(
        self, event_id: UUID, clip: Path, attributes: dict[str, Any], now: float
    ) -> None:
        """Attach the clip and mark the event closed (a new revision to send)."""
        rows = self._query(
            "SELECT payload, ended_at FROM events WHERE event_id = ?", (str(event_id),)
        )
        payload = json.loads(rows[0]["payload"])
        payload["status"] = EventStatus.CLOSED.value
        payload["ended_at"] = _iso(rows[0]["ended_at"])
        payload["attributes"] = {**payload.get("attributes", {}), **attributes}
        self._tx(
            [
                self._media_insert(event_id, MediaKind.CLIP, clip),
                (
                    "UPDATE events SET payload = ?, revision = revision + 1, clip_due_at = NULL,"
                    " next_attempt_at = 0, updated_at = ? WHERE event_id = ?",
                    (json.dumps(payload), now, str(event_id)),
                ),
            ]
        )

    def postpone_clip(self, event_id: UUID, until: float) -> None:
        self._tx([("UPDATE events SET clip_due_at = ? WHERE event_id = ?", (until, str(event_id)))])

    def recover_unfinished(self, now: float) -> int:
        """After a restart, events that were still open can no longer end normally:
        end them at their last update so they get a clip and close."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE events SET ended_at = updated_at, clip_due_at = ?"
                " WHERE ended_at IS NULL AND dropped = 0",
                (now,),
            )
            return cursor.rowcount

    # --- sending ----------------------------------------------------------------------

    def due_events(self, now: float, limit: int) -> list[PendingEvent]:
        rows = self._query(
            "SELECT event_id, revision, payload FROM events"
            " WHERE sent_revision < revision AND dropped = 0 AND next_attempt_at <= ?"
            " ORDER BY created_at LIMIT ?",
            (now, limit),
        )
        events = []
        for row in rows:
            media = [
                MediaFile(
                    MediaKind(m["kind"]),
                    Path(m["path"]),
                    m["size_bytes"],
                    m["sha256"],
                    m["media_key"],
                    bool(m["on_disk"]),
                )
                for m in self._query(
                    "SELECT * FROM media WHERE event_id = ? ORDER BY kind DESC", (row["event_id"],)
                )
            ]
            events.append(
                PendingEvent(
                    UUID(row["event_id"]), row["revision"], json.loads(row["payload"]), media
                )
            )
        return events

    def set_media_key(self, event_id: UUID, kind: MediaKind, media_key: str) -> None:
        self._tx(
            [
                (
                    "UPDATE media SET media_key = ? WHERE event_id = ? AND kind = ?",
                    (media_key, str(event_id), kind.value),
                )
            ]
        )

    def mark_sent(self, event_id: UUID, revision: int, now: float) -> None:
        self._tx(
            [
                (
                    "UPDATE events SET sent_revision = MAX(sent_revision, ?), attempts = 0,"
                    " last_error = NULL, updated_at = ? WHERE event_id = ?",
                    (revision, now, str(event_id)),
                )
            ]
        )

    def mark_retry(self, event_ids: Iterable[UUID], error: str, now: float) -> None:
        statements = []
        for event_id in event_ids:
            rows = self._query("SELECT attempts FROM events WHERE event_id = ?", (str(event_id),))
            attempts = rows[0]["attempts"] + 1
            # Exponential backoff with jitter so many devices don't retry in step.
            delay = min(MAX_BACKOFF_SECONDS, 2**attempts) * (0.5 + random.random() / 2)  # noqa: S311
            statements.append(
                (
                    "UPDATE events SET attempts = ?, next_attempt_at = ?, last_error = ?"
                    " WHERE event_id = ?",
                    (attempts, now + delay, error[:500], str(event_id)),
                )
            )
        self._tx(statements)

    def mark_dropped(self, event_id: UUID, error: str) -> None:
        """Give up on an event the cloud will never accept (it is kept for inspection)."""
        self._tx(
            [
                (
                    "UPDATE events SET dropped = 1, clip_due_at = NULL, last_error = ?"
                    " WHERE event_id = ?",
                    (error[:500], str(event_id)),
                )
            ]
        )

    def pending_count(self) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM events WHERE dropped = 0"
            " AND (sent_revision < revision OR clip_due_at IS NOT NULL)"
        )
        return rows[0]["n"]

    # --- disk space -------------------------------------------------------------------

    def cleanup(self, quota_bytes: int, sent_before: float) -> int:
        """Free disk space; returns the number of files deleted.

        1. Media of events that are fully sent and older than `sent_before`.
        2. If still over quota: media of other fully-sent events, oldest first.
        3. If still over quota: the oldest unsent events are dropped (logged as an error by
           the caller). This only happens after a very long outage.
        """
        done = "e.sent_revision = e.revision AND e.clip_due_at IS NULL AND e.ended_at IS NOT NULL"
        deleted = self._delete_media(
            f"SELECT m.event_id, m.kind, m.path, m.size_bytes FROM media m JOIN events e"  # noqa: S608
            f" USING (event_id) WHERE m.on_disk = 1 AND {done} AND e.updated_at < ?",
            (sent_before,),
        )
        for condition, drop in ((done, False), ("e.dropped = 0", True)):
            over = self.disk_bytes() - quota_bytes
            if over <= 0:
                break
            deleted += self._delete_media(
                f"SELECT m.event_id, m.kind, m.path, m.size_bytes FROM media m JOIN events e"  # noqa: S608
                f" USING (event_id) WHERE m.on_disk = 1 AND {condition} ORDER BY e.created_at",
                (),
                limit_bytes=over,
                drop=drop,
            )
        return deleted

    def disk_bytes(self) -> int:
        rows = self._query("SELECT COALESCE(SUM(size_bytes), 0) AS n FROM media WHERE on_disk = 1")
        return rows[0]["n"]

    def _delete_media(
        self, sql: str, params: tuple, limit_bytes: int | None = None, drop: bool = False
    ) -> int:
        freed = deleted = 0
        for row in self._query(sql, params):
            if limit_bytes is not None and freed >= limit_bytes:
                break
            path = Path(row["path"])
            path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                path.parent.rmdir()  # the event's folder, once empty
            statements = [
                (
                    "UPDATE media SET on_disk = 0 WHERE event_id = ? AND kind = ?",
                    (row["event_id"], row["kind"]),
                )
            ]
            if drop:
                statements.append(
                    (
                        "UPDATE events SET dropped = 1, clip_due_at = NULL,"
                        " last_error = 'deleted: local disk quota' WHERE event_id = ?",
                        (row["event_id"],),
                    )
                )
            self._tx(statements)
            freed += row["size_bytes"]
            deleted += 1
        return deleted
