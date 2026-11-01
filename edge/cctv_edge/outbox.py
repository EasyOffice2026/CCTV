"""Local outbox: every event and its media are written here before anything is sent.

The sync loop sends from the outbox and only marks an event sent once the cloud accepts
it, so events survive internet outages and restarts. Each change to an event (open ->
closed) bumps its revision; the cloud upserts by `event_id`, so resending is harmless.

Attendance sightings, count buckets and heatmap buckets wait here too. They are small and
have no media: sightings are deleted once the cloud has answered, count and heatmap buckets
are kept a few hours after sending so a partial bucket (camera restarted mid-bucket) can be
added to them and the full totals sent again.
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
from cctv_contracts.v1 import CountBucketIn, HeatmapBucketIn, StaffSighting

# Count columns added after the first release; older outboxes get them on start.
COUNT_COLUMNS_ADDED = ("staff_crossings", "waits", "wait_total_seconds", "wait_max_seconds")


def _merge_added(column: str) -> str:
    """Merging a partial bucket: sums, except the longest wait."""
    if column == "wait_max_seconds":
        combined = f"MAX({column}, excluded.{column})"
    else:
        combined = f"{column} + excluded.{column}"
    return (
        f" {column} = CASE WHEN {column} IS NULL THEN excluded.{column}"
        f" WHEN excluded.{column} IS NULL THEN {column} ELSE {combined} END,"
    )


_ADDED_UPDATE = "".join(_merge_added(c) for c in COUNT_COLUMNS_ADDED)

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
CREATE TABLE IF NOT EXISTS sightings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payload         TEXT NOT NULL,           -- StaffSighting JSON
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS counts (
    camera_id        TEXT NOT NULL,
    zone_id          TEXT NOT NULL DEFAULT '',  -- '' for the whole camera
    bucket_start     REAL NOT NULL,
    bucket_seconds   INTEGER NOT NULL,
    in_count         INTEGER NOT NULL DEFAULT 0,
    out_count        INTEGER NOT NULL DEFAULT 0,
    max_occupancy    INTEGER,
    occupied_seconds INTEGER,
    staff_crossings  INTEGER,
    waits            INTEGER,
    wait_total_seconds INTEGER,
    wait_max_seconds INTEGER,
    sent             INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (camera_id, zone_id, bucket_start)
);
CREATE TABLE IF NOT EXISTS heatmaps (
    camera_id        TEXT NOT NULL,
    bucket_start     REAL NOT NULL,
    bucket_seconds   INTEGER NOT NULL,
    columns          INTEGER NOT NULL,
    rows             INTEGER NOT NULL,
    cells            TEXT NOT NULL,           -- {"cell index": seconds} JSON
    sent             INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (camera_id, bucket_start)
);
"""

MAX_BACKOFF_SECONDS = 300
# Sent count buckets are kept this long, to merge late partial buckets into.
KEEP_SENT_COUNTS_SECONDS = 6 * 3600
# Sightings the cloud never took (very long outage) are given up after this long.
MAX_SIGHTING_AGE_SECONDS = 30 * 86400

CountKey = tuple[str, str, float]  # camera id, zone id ('' for none), bucket start
HeatmapKey = tuple[str, float]  # camera id, bucket start


def _backoff(attempts: int) -> float:
    # Exponential backoff with jitter so many devices don't retry in step.
    return min(MAX_BACKOFF_SECONDS, 2**attempts) * (0.5 + random.random() / 2)  # noqa: S311


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
            # Boxes installed before these columns existed (plan 10.2).
            have = {row[1] for row in self._db.execute("PRAGMA table_info(counts)")}
            for column in COUNT_COLUMNS_ADDED:
                if column not in have:
                    self._db.execute(f"ALTER TABLE counts ADD COLUMN {column} INTEGER")

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
            statements.append(
                (
                    "UPDATE events SET attempts = ?, next_attempt_at = ?, last_error = ?"
                    " WHERE event_id = ?",
                    (attempts, now + _backoff(attempts), error[:500], str(event_id)),
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

    # --- attendance sightings -----------------------------------------------------------

    def add_sightings(self, sightings: Iterable[StaffSighting], now: float) -> None:
        self._tx(
            (
                "INSERT INTO sightings (payload, created_at) VALUES (?, ?)",
                (s.model_dump_json(), now),
            )
            for s in sightings
        )

    def due_sightings(self, now: float, limit: int) -> list[tuple[int, StaffSighting]]:
        rows = self._query(
            "SELECT id, payload FROM sightings WHERE next_attempt_at <= ? ORDER BY id LIMIT ?",
            (now, limit),
        )
        return [(row["id"], StaffSighting.model_validate_json(row["payload"])) for row in rows]

    def delete_sightings(self, ids: Iterable[int]) -> None:
        self._tx(("DELETE FROM sightings WHERE id = ?", (i,)) for i in ids)

    def retry_sightings(self, ids: Iterable[int], now: float) -> None:
        statements = []
        for i in ids:
            rows = self._query("SELECT attempts FROM sightings WHERE id = ?", (i,))
            if rows:
                attempts = rows[0]["attempts"] + 1
                statements.append(
                    (
                        "UPDATE sightings SET attempts = ?, next_attempt_at = ? WHERE id = ?",
                        (attempts, now + _backoff(attempts), i),
                    )
                )
        self._tx(statements)

    # --- count buckets ------------------------------------------------------------------

    def add_counts(self, buckets: Iterable[CountBucketIn]) -> None:
        """Store finished buckets. A bucket already here (a partial one from before a
        restart) is added to, and sent again with the combined totals."""
        self._tx(
            (
                # Fixed column names only (_ADDED_UPDATE); values are parameters.
                "INSERT INTO counts (camera_id, zone_id, bucket_start, bucket_seconds,"  # noqa: S608
                " in_count, out_count, max_occupancy, occupied_seconds,"
                " staff_crossings, waits, wait_total_seconds, wait_max_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (camera_id, zone_id, bucket_start) DO UPDATE SET"
                " in_count = in_count + excluded.in_count,"
                " out_count = out_count + excluded.out_count,"
                " max_occupancy = CASE WHEN max_occupancy IS NULL THEN excluded.max_occupancy"
                "   WHEN excluded.max_occupancy IS NULL THEN max_occupancy"
                "   ELSE MAX(max_occupancy, excluded.max_occupancy) END,"
                " occupied_seconds = CASE WHEN occupied_seconds IS NULL"
                "   THEN excluded.occupied_seconds"
                "   WHEN excluded.occupied_seconds IS NULL THEN occupied_seconds"
                "   ELSE MIN(bucket_seconds, occupied_seconds + excluded.occupied_seconds) END,"
                + _ADDED_UPDATE
                + " sent = 0, attempts = 0, next_attempt_at = 0",
                (
                    str(b.camera_id),
                    str(b.zone_id) if b.zone_id else "",
                    b.bucket_start.timestamp(),
                    b.bucket_seconds,
                    b.in_count,
                    b.out_count,
                    b.max_occupancy,
                    b.occupied_seconds,
                    b.staff_crossings,
                    b.waits,
                    b.wait_total_seconds,
                    b.wait_max_seconds,
                ),
            )
            for b in buckets
        )

    def due_counts(self, now: float, limit: int) -> list[tuple[CountKey, CountBucketIn]]:
        rows = self._query(
            "SELECT * FROM counts WHERE sent = 0 AND next_attempt_at <= ?"
            " ORDER BY bucket_start LIMIT ?",
            (now, limit),
        )
        return [
            (
                (row["camera_id"], row["zone_id"], row["bucket_start"]),
                CountBucketIn(
                    camera_id=row["camera_id"],
                    zone_id=row["zone_id"] or None,
                    bucket_start=datetime.fromtimestamp(row["bucket_start"], timezone.utc),
                    bucket_seconds=row["bucket_seconds"],
                    in_count=row["in_count"],
                    out_count=row["out_count"],
                    max_occupancy=row["max_occupancy"],
                    occupied_seconds=row["occupied_seconds"],
                    **{c: row[c] for c in COUNT_COLUMNS_ADDED},
                ),
            )
            for row in rows
        ]

    def mark_counts_sent(self, keys: Iterable[CountKey]) -> None:
        self._tx(
            (
                "UPDATE counts SET sent = 1, attempts = 0"
                " WHERE camera_id = ? AND zone_id = ? AND bucket_start = ?",
                key,
            )
            for key in keys
        )

    def retry_counts(self, keys: Iterable[CountKey], now: float) -> None:
        statements = []
        for key in keys:
            rows = self._query(
                "SELECT attempts FROM counts WHERE camera_id = ? AND zone_id = ?"
                " AND bucket_start = ?",
                key,
            )
            if rows:
                attempts = rows[0]["attempts"] + 1
                statements.append(
                    (
                        "UPDATE counts SET attempts = ?, next_attempt_at = ?"
                        " WHERE camera_id = ? AND zone_id = ? AND bucket_start = ?",
                        (attempts, now + _backoff(attempts), *key),
                    )
                )
        self._tx(statements)

    # --- heatmap buckets ----------------------------------------------------------------

    def add_heatmaps(self, buckets: Iterable[HeatmapBucketIn]) -> None:
        """Store finished buckets. A bucket already here (a partial one from before a
        restart) is added to, cell by cell, and sent again with the combined totals."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for b in buckets:
                    key = (str(b.camera_id), b.bucket_start.timestamp())
                    cells = {str(i): s for i, s in b.cells}
                    row = self._db.execute(
                        "SELECT columns, rows, cells FROM heatmaps"
                        " WHERE camera_id = ? AND bucket_start = ?",
                        key,
                    ).fetchone()
                    if row is not None and (row["columns"], row["rows"]) == (b.columns, b.rows):
                        for i, s in json.loads(row["cells"]).items():
                            cells[i] = cells.get(i, 0) + s
                    self._db.execute(
                        "INSERT OR REPLACE INTO heatmaps (camera_id, bucket_start,"
                        " bucket_seconds, columns, rows, cells) VALUES (?, ?, ?, ?, ?, ?)",
                        (*key, b.bucket_seconds, b.columns, b.rows, json.dumps(cells)),
                    )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def due_heatmaps(self, now: float, limit: int) -> list[tuple[HeatmapKey, HeatmapBucketIn]]:
        rows = self._query(
            "SELECT * FROM heatmaps WHERE sent = 0 AND next_attempt_at <= ?"
            " ORDER BY bucket_start LIMIT ?",
            (now, limit),
        )
        return [
            (
                (row["camera_id"], row["bucket_start"]),
                HeatmapBucketIn(
                    camera_id=row["camera_id"],
                    bucket_start=datetime.fromtimestamp(row["bucket_start"], timezone.utc),
                    bucket_seconds=row["bucket_seconds"],
                    columns=row["columns"],
                    rows=row["rows"],
                    cells=sorted((int(i), s) for i, s in json.loads(row["cells"]).items()),
                ),
            )
            for row in rows
        ]

    def mark_heatmaps_sent(self, keys: Iterable[HeatmapKey]) -> None:
        self._tx(
            (
                "UPDATE heatmaps SET sent = 1, attempts = 0"
                " WHERE camera_id = ? AND bucket_start = ?",
                key,
            )
            for key in keys
        )

    def retry_heatmaps(self, keys: Iterable[HeatmapKey], now: float) -> None:
        statements = []
        for key in keys:
            rows = self._query(
                "SELECT attempts FROM heatmaps WHERE camera_id = ? AND bucket_start = ?", key
            )
            if rows:
                attempts = rows[0]["attempts"] + 1
                statements.append(
                    (
                        "UPDATE heatmaps SET attempts = ?, next_attempt_at = ?"
                        " WHERE camera_id = ? AND bucket_start = ?",
                        (attempts, now + _backoff(attempts), *key),
                    )
                )
        self._tx(statements)

    def cleanup_aggregates(self, now: float) -> None:
        self._tx(
            [
                (
                    "DELETE FROM heatmaps WHERE sent = 1 AND bucket_start < ?",
                    (now - KEEP_SENT_COUNTS_SECONDS,),
                ),
                (
                    "DELETE FROM counts WHERE sent = 1 AND bucket_start < ?",
                    (now - KEEP_SENT_COUNTS_SECONDS,),
                ),
                (
                    "DELETE FROM sightings WHERE created_at < ?",
                    (now - MAX_SIGHTING_AGE_SECONDS,),
                ),
            ]
        )

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
