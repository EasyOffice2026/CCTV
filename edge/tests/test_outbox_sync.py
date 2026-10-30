import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cctv_contracts.enums import MediaKind
from cctv_edge.outbox import Outbox
from cctv_edge.sync import Syncer


@pytest.fixture
def outbox(tmp_path: Path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    yield box
    box.close()


def write(path: Path, size: int = 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff" * size)
    return path


def payload(event_id=None, started_at: float | None = None) -> dict:
    started = started_at if started_at is not None else time.time()
    return {
        "event_id": str(event_id or uuid.uuid4()),
        "camera_id": str(uuid.uuid4()),
        "zone_id": None,
        "feature": "security",
        "event_type": "after_hours_person",
        "severity": "critical",
        "status": "open",
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "confidence": 0.9,
        "track_id": None,
        "attributes": {},
    }


def open_and_close(outbox: Outbox, tmp_path: Path, now: float) -> uuid.UUID:
    p = payload(started_at=now)
    event_id = uuid.UUID(p["event_id"])
    outbox.open_event(p, write(tmp_path / str(event_id) / "snapshot.jpg"), now)
    outbox.end_event(event_id, now + 5, now + 20, now)
    [job] = outbox.clips_due(now + 20)
    outbox.close_event(job.event_id, write(tmp_path / str(event_id) / "clip.mp4", 500), {}, now)
    return event_id


def test_open_then_closed_event_is_synced(outbox, client, cloud, tmp_path):
    now = time.time()
    p = payload(started_at=now)
    event_id = uuid.UUID(p["event_id"])
    outbox.open_event(p, write(tmp_path / "snap.jpg"), now)

    assert Syncer(outbox, client).sync_once(now) == 1
    sent = cloud.events[str(event_id)]
    assert sent["status"] == "open"
    assert cloud.objects[sent["snapshot_key"]] == b"\xff" * 100
    assert outbox.pending_count() == 0
    assert Syncer(outbox, client).sync_once(now) == 0  # nothing new

    outbox.end_event(event_id, now + 5, now + 20, now)
    assert outbox.pending_count() == 1  # waiting for its clip
    assert outbox.clips_due(now + 10) == []
    [job] = outbox.clips_due(now + 20)
    assert job.camera_id == uuid.UUID(p["camera_id"])
    assert job.started_at == pytest.approx(now)
    outbox.close_event(event_id, write(tmp_path / "clip.mp4", 500), {"duration_s": 5}, now)

    assert Syncer(outbox, client).sync_once(now) == 1
    sent = cloud.events[str(event_id)]
    assert sent["status"] == "closed"
    assert sent["attributes"] == {"duration_s": 5}
    assert cloud.objects[sent["clip_key"]] == b"\xff" * 500
    # The snapshot was not uploaded a second time.
    assert sum(k.endswith("snapshot.jpg") for k in cloud.objects) == 1
    assert outbox.pending_count() == 0


def test_nothing_is_lost_while_offline(outbox, client, cloud, tmp_path):
    now = time.time()
    ids = [open_and_close(outbox, tmp_path, now) for _ in range(3)]
    cloud.down = True
    syncer = Syncer(outbox, client)
    assert syncer.sync_once(now) == 0
    assert outbox.pending_count() == 3
    assert syncer.sync_once(now) == 0  # backing off: not even tried
    assert outbox.due_events(now, 10) == []

    cloud.down = False
    assert syncer.sync_once(now + 1000) == 3
    assert set(cloud.events) == {str(i) for i in ids}
    assert all(e["status"] == "closed" for e in cloud.events.values())
    assert outbox.pending_count() == 0


def test_backoff_grows(outbox, client, cloud, tmp_path):
    now = time.time()
    open_and_close(outbox, tmp_path, now)
    cloud.down = True
    syncer = Syncer(outbox, client)
    t = now
    delays = []
    for _ in range(5):
        syncer.sync_once(t)
        [row] = outbox._query("SELECT next_attempt_at FROM events")
        delays.append(row["next_attempt_at"] - t)
        t = row["next_attempt_at"]
    assert delays[-1] > delays[0]
    assert max(delays) <= 300


def test_rejected_events_are_dropped_not_retried(outbox, client, cloud, tmp_path):
    now = time.time()
    bad = open_and_close(outbox, tmp_path, now)
    good = open_and_close(outbox, tmp_path, now)
    cloud.reject.add(str(bad))
    assert Syncer(outbox, client).sync_once(now) == 1
    assert set(cloud.events) == {str(good)}
    assert outbox.pending_count() == 0
    assert outbox.due_events(now + 10_000, 10) == []


def test_missing_media_file_drops_the_event(outbox, client, cloud, tmp_path):
    now = time.time()
    p = payload(started_at=now)
    snap = write(tmp_path / "snap.jpg")
    outbox.open_event(p, snap, now)
    snap.unlink()
    assert Syncer(outbox, client).sync_once(now) == 0
    assert cloud.events == {}
    assert outbox.pending_count() == 0


def test_recover_unfinished_after_restart(outbox, tmp_path):
    now = time.time()
    p = payload(started_at=now)
    outbox.open_event(p, write(tmp_path / "snap.jpg"), now)
    assert outbox.recover_unfinished(now + 100) == 1
    [job] = outbox.clips_due(now + 100)
    assert job.event_id == uuid.UUID(p["event_id"])
    assert job.ended_at == pytest.approx(now)


def test_cleanup_deletes_old_sent_media_first(outbox, client, tmp_path):
    now = time.time()
    sent = open_and_close(outbox, tmp_path, now)
    Syncer(outbox, client).sync_once(now)
    unsent = open_and_close(outbox, tmp_path, now)
    assert outbox.disk_bytes() == 1200

    # Over quota: sent media goes first, the unsent event is kept.
    assert outbox.cleanup(quota_bytes=700, sent_before=now - 3600) == 2
    assert not (tmp_path / str(sent)).exists()
    assert (tmp_path / str(unsent) / "clip.mp4").exists()
    assert outbox.pending_count() == 1

    # Still over quota: the oldest unsent event is dropped as a last resort.
    assert outbox.cleanup(quota_bytes=100, sent_before=now - 3600) >= 1
    assert outbox.pending_count() == 0


def test_cleanup_removes_sent_media_after_retention(outbox, client, tmp_path):
    now = time.time()
    event_id = open_and_close(outbox, tmp_path, now)
    Syncer(outbox, client).sync_once(now)
    assert outbox.cleanup(quota_bytes=10**9, sent_before=now - 3600) == 0
    assert outbox.cleanup(quota_bytes=10**9, sent_before=now + 1) == 2
    assert not (tmp_path / str(event_id)).exists()


def test_media_keys_are_reused_after_partial_failure(outbox, client, cloud, tmp_path):
    now = time.time()
    event_id = open_and_close(outbox, tmp_path, now)
    syncer = Syncer(outbox, client)
    real = client.send_events

    def fail(_batch):
        cloud.down = True
        try:
            return real(_batch)
        finally:
            cloud.down = False

    client.send_events = fail
    assert syncer.sync_once(now) == 0
    uploads = len(cloud.objects)
    assert uploads == 2
    client.send_events = real
    assert syncer.sync_once(now + 1000) == 1
    assert len(cloud.objects) == uploads  # media was not uploaded again
    assert cloud.events[str(event_id)]["status"] == "closed"


def test_media_kinds_round_trip(outbox, tmp_path):
    now = time.time()
    open_and_close(outbox, tmp_path, now)
    [event] = outbox.due_events(now, 10)
    assert {m.kind for m in event.media} == {MediaKind.SNAPSHOT, MediaKind.CLIP}
