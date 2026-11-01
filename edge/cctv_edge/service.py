"""The edge service: one worker per camera, plus clip, sync, config, heartbeat, remote and
face-template loops."""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from uuid import UUID

import numpy as np

from cctv_contracts.enums import EventStatus
from cctv_contracts.v1 import (
    CameraConfig,
    CameraStatus,
    ClipSettings,
    CountBucketIn,
    EdgeConfig,
    Heartbeat,
    StaffSighting,
)
from cctv_edge import __version__, system
from cctv_edge.cloud import CloudClient, CloudError
from cctv_edge.config_store import ConfigStore
from cctv_edge.credentials import Credential, redact, with_credentials
from cctv_edge.detection import BUILTIN_DETECTORS, DetectorFactory
from cctv_edge.face import FaceAnalyzer, FaceGallery
from cctv_edge.ffmpeg import FfmpegError
from cctv_edge.go2rtc import Go2rtc
from cctv_edge.media import build_clip, save_snapshot, still_clip
from cctv_edge.models.classifier import ClassifierFactory
from cctv_edge.models.manifest import ModelTask
from cctv_edge.models.registry import ModelRegistry
from cctv_edge.outbox import Outbox
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.remote import RemoteWorker
from cctv_edge.ringbuffer import SegmentRecorder, list_segments, segments_between
from cctv_edge.rules import EventOpened, RuleOutput
from cctv_edge.schedule import BranchSchedule
from cctv_edge.settings import EdgeSettings
from cctv_edge.streams import FrameReader, StreamHealth
from cctv_edge.sync import Syncer
from cctv_edge.token_store import TokenStore

log = logging.getLogger(__name__)

# Allowed clock difference with the cloud before warning (timestamps come from this box).
MAX_CLOCK_DRIFT_SECONDS = 5
# A loop step taking longer than this counts as hung (a 100 MB clip on a slow uplink can
# take several minutes, so this is generous).
LOOP_STALL_SECONDS = 900


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _stream_key(camera: CameraConfig) -> tuple[str, str | None, str | None]:
    return (camera.rtsp_main, camera.rtsp_sub, camera.credentials_ref)


class CameraWorker:
    """Ring-buffer recording of the main stream, and analysis of the sub-stream."""

    def __init__(self, service: "EdgeService", camera: CameraConfig, pipeline: CameraPipeline):
        self.service = service
        self.camera = camera
        self.pipeline = pipeline
        settings = service.settings
        self.health = StreamHealth(settings.analysis_fps, settings.stale_after_seconds)
        # Times the analysis stream ended and was reopened (plan 9.2: 48 h without drops).
        self.restarts = 0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader: FrameReader | None = None
        self.recorder: SegmentRecorder | None = None
        # Fixed for the worker's life: a change restarts the worker (see apply_config).
        self.detail = pipeline.needs_detail

    def start(self) -> None:
        settings = self.service.settings
        main = self.service.camera_url(self.camera, self.camera.rtsp_main)
        analysis = self.service.camera_url(
            self.camera, self.camera.rtsp_sub or self.camera.rtsp_main
        )
        self.recorder = SegmentRecorder(
            settings.ffmpeg,
            main,
            settings.ring_dir / str(self.camera.id),
            settings.segment_seconds,
            settings.ring_seconds,
        )
        self.recorder.start()
        self._thread = threading.Thread(
            target=self._run, args=(analysis,), name=f"camera-{self.camera.id}", daemon=True
        )
        self._thread.start()

    def _run(self, url: str) -> None:
        settings = self.service.settings
        width, height = self.service.analysis_size(self.detail)
        backoff = 1.0
        while not self._stop.is_set():
            reader = self._reader = FrameReader(
                settings.ffmpeg, url, settings.analysis_fps, width, height
            )
            started = time.monotonic()
            try:
                for ts, frame in reader.frames():
                    if self._stop.is_set():
                        break
                    self.health.frame(ts)
                    self.process(frame, ts)
            except Exception:
                log.exception("camera %s: analysis failed", self.camera.name)
            if self._stop.is_set():
                break
            error = reader.stderr_tail[-1] if reader.stderr_tail else "stream ended"
            self.health.error(error)
            self.restarts += 1
            log.warning("camera %s (%s): %s", self.camera.name, redact(url), error)
            backoff = 1.0 if time.monotonic() - started > 60 else min(backoff * 2, 30)
            self._stop.wait(backoff)

    def process(self, frame: np.ndarray, ts: float) -> None:
        with self.lock:
            result = self.pipeline.process(frame, ts)
        self.service.handle_outputs(self.camera, result.outputs, frame)
        self.service.store_aggregates(result.counts, result.sightings)

    def replace_pipeline(self, camera: CameraConfig, pipeline: CameraPipeline) -> None:
        """Apply new zones/features without restarting the stream. Open events are closed
        first, because the new rules start with no state."""
        with self.lock:
            outputs = self.pipeline.engine.close_all()
            counts = self.pipeline.flush_counts()
            self.camera, self.pipeline = camera, pipeline
        self.service.handle_outputs(camera, outputs, None)
        self.service.store_aggregates(counts, [])

    def stop(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._reader.stop()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.recorder is not None:
            self.recorder.stop()
        with self.lock:
            outputs = self.pipeline.engine.close_all()
            counts = self.pipeline.flush_counts()
        self.service.handle_outputs(self.camera, outputs, None)
        self.service.store_aggregates(counts, [])

    def status(self, now: float) -> CameraStatus:
        report = self.health.report(now)
        error = report.error or (self.recorder.last_error if self.recorder else None)
        return CameraStatus(
            camera_id=self.camera.id,
            health=report.health,
            fps=report.fps,
            last_frame_at=report.last_frame_at,
            error=error[:500] if error else None,
            stream_restarts=self.restarts,
        )


class EdgeService:
    def __init__(
        self,
        settings: EdgeSettings,
        client: CloudClient,
        outbox: Outbox,
        *,
        credentials: Mapping[str, Credential] | None = None,
        detector_factories: Mapping[str, DetectorFactory] | None = None,
        start_streams: bool = True,
        go2rtc: Go2rtc | None = None,
        models: ModelRegistry | None = None,
        classifier_factories: Mapping[ModelTask, ClassifierFactory] | None = None,
        face_analyzer: FaceAnalyzer | None = None,
        tokens: TokenStore | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.tokens = tokens
        self.outbox = outbox
        self.syncer = Syncer(outbox, client)
        self.store = ConfigStore(settings.config_path)
        self.credentials = dict(credentials or {})
        # Models are loaded once here; pipelines rebuilt on each config only wrap them.
        self.models = models or ModelRegistry(settings.models_path, settings.model_providers)
        if detector_factories is None:
            detector_factories = {
                **{
                    name: BUILTIN_DETECTORS[name]
                    for name in settings.detectors
                    if name in BUILTIN_DETECTORS
                },
                **self.models.detector_factories(),
            }
        self.detector_factories = dict(detector_factories)
        self.classifier_factories = dict(
            self.models.classifier_factories()
            if classifier_factories is None
            else classifier_factories
        )
        self.face_analyzer = face_analyzer or self.models.face_analyzer()
        # Enrolled staff faces of the branch: in memory only, refetched from the cloud.
        self.gallery = FaceGallery()
        self._templates_etag: str | None = None
        # False in tests: workers are created but no ffmpeg processes are started.
        self.start_streams = start_streams

        self.config: EdgeConfig | None = None
        self.schedule = BranchSchedule([], "Asia/Kuwait")
        self.workers: dict[UUID, CameraWorker] = {}
        self._apply_lock = threading.Lock()
        self._stop = threading.Event()
        self._sync_wanted = threading.Event()
        self._config_wanted = threading.Event()
        self._templates_wanted = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ticks: dict[str, tuple[float, float]] = {}  # loop -> (last step done, interval)
        self._started = time.monotonic()
        self.remote = RemoteWorker(
            client,
            go2rtc or Go2rtc(settings.go2rtc_url),
            lambda: self.config,
            self.credentials,
            settings.ffmpeg,
            settings.remote_workers,
            face_analyzer=self.face_analyzer,
        )
        self._remote_backoff = 0.0

    # --- config -----------------------------------------------------------------------

    def camera_url(self, camera: CameraConfig, url: str) -> str:
        credential = None
        if camera.credentials_ref:
            credential = self.credentials.get(camera.credentials_ref)
            if credential is None:
                log.warning("no local credentials named %r", camera.credentials_ref)
        return with_credentials(url, credential)

    def analysis_size(self, detail: bool) -> tuple[int, int]:
        s = self.settings
        return (
            (s.detail_width, s.detail_height) if detail else (s.analysis_width, s.analysis_height)
        )

    def apply_config(self, config: EdgeConfig) -> None:
        """Start, update or stop camera workers to match `config`, without a restart.
        Cameras with no enabled feature that can run are not streamed or analysed at all."""
        with self._apply_lock:
            previous = self.config
            schedule_changed = previous is None or (previous.timezone, previous.working_hours) != (
                config.timezone,
                config.working_hours,
            )
            if schedule_changed:
                self.schedule = BranchSchedule(config.working_hours, config.timezone)
            self.config = config

            wanted: dict[UUID, tuple[CameraConfig, CameraPipeline]] = {}
            for camera in config.cameras:
                pipeline = CameraPipeline(
                    camera,
                    self.schedule,
                    self.detector_factories,
                    self.settings.max_event_seconds,
                    classifiers=self.classifier_factories,
                    face_analyzer=self.face_analyzer,
                    gallery=self.gallery,
                )
                if pipeline.active:
                    wanted[camera.id] = (camera, pipeline)

            for camera_id, worker in list(self.workers.items()):
                target = wanted.get(camera_id)
                if (
                    target is None
                    or _stream_key(target[0]) != _stream_key(worker.camera)
                    or target[1].needs_detail != worker.detail  # analysis size changed
                ):
                    worker.stop()
                    del self.workers[camera_id]
            for camera_id, (camera, pipeline) in wanted.items():
                worker = self.workers.get(camera_id)
                if worker is None:
                    worker = self.workers[camera_id] = CameraWorker(self, camera, pipeline)
                    if self.start_streams:
                        worker.start()
                elif worker.camera != camera or schedule_changed:
                    worker.replace_pipeline(camera, pipeline)
            log.info(
                "config v%d applied: %d of %d cameras active",
                config.version,
                len(self.workers),
                len(config.cameras),
            )

    def poll_config(self) -> None:
        current = self.config.version if self.config else None
        config = self.client.fetch_config(current)
        if config is not None and config.version != current:
            self.store.save(config)
            self.apply_config(config)

    def refresh_face_templates(self, now: float) -> None:
        """Fetch the enrolled staff templates (only with a face model on the box)."""
        if self.face_analyzer is None:
            return
        result = self.client.fetch_face_templates(self._templates_etag)
        if result is None:
            return  # unchanged
        templates, etag = result
        count = self.gallery.replace(templates.templates, self.face_analyzer.model_version)
        self._templates_etag = etag
        log.info(
            "face templates: %d staff enrolled for %s", count, self.face_analyzer.model_version
        )

    # --- events -----------------------------------------------------------------------

    @property
    def clip_settings(self) -> ClipSettings:
        return self.config.clip if self.config else ClipSettings()

    def handle_outputs(
        self, camera: CameraConfig, outputs: Sequence[RuleOutput], frame: np.ndarray | None
    ) -> None:
        now = time.time()
        for output in outputs:
            # One failing event (e.g. disk full) must not stop the camera's analysis.
            try:
                if isinstance(output, EventOpened):
                    if frame is None:  # rules only open events while processing a frame
                        continue
                    self._open_event(camera, output, frame, now)
                else:
                    due = (
                        output.ended_at
                        + self.clip_settings.post_seconds
                        + 2 * self.settings.segment_seconds
                    )
                    self.outbox.end_event(output.event_id, output.ended_at, due, now)
                    log.info("event %s ended; clip due in %.0fs", output.event_id, due - now)
            except Exception:
                log.exception("camera %s: could not record event %s", camera.name, output.event_id)

    def store_aggregates(
        self, counts: Sequence[CountBucketIn], sightings: Sequence[StaffSighting]
    ) -> None:
        now = time.time()
        try:
            if counts:
                self.outbox.add_counts(counts)
            if sightings:
                self.outbox.add_sightings(sightings, now)
                self._sync_wanted.set()
        except Exception:
            log.exception("could not store %d counts / %d sightings", len(counts), len(sightings))

    def _open_event(
        self, camera: CameraConfig, event: EventOpened, frame: np.ndarray, now: float
    ) -> None:
        snapshot = self.settings.media_dir / str(event.event_id) / "snapshot.jpg"
        save_snapshot(frame, event.boxes, snapshot)
        attributes = dict(event.rule.attributes)
        if event.rule.min_count > 1:
            attributes["people"] = len(event.boxes)  # in the zone when the event opened
        if event.continues:
            attributes["continues"] = str(event.continues)
        payload = {
            "event_id": str(event.event_id),
            "camera_id": str(camera.id),
            "zone_id": str(event.zone_id) if event.zone_id else None,
            "feature": event.rule.feature.value,
            "event_type": event.rule.event_type.value,
            "severity": event.rule.severity.value,
            "status": EventStatus.OPEN.value,
            "started_at": _iso(event.started_at),
            "confidence": round(event.confidence, 3),
            "track_id": str(event.track_id) if event.track_id is not None else None,
            "attributes": attributes,
        }
        self.outbox.open_event(payload, snapshot, now)
        self._sync_wanted.set()
        log.info(
            "event %s opened: %s on %s", event.event_id, event.rule.event_type.value, camera.name
        )

    def run_clip_jobs(self, now: float) -> int:
        """Cut clips for events whose post-roll is recorded. Returns how many were closed."""
        clip = self.clip_settings
        closed = 0
        for job in self.outbox.clips_due(now):
            path = self.settings.media_dir / str(job.event_id) / "clip.mp4"
            start, end = job.started_at - clip.pre_seconds, job.ended_at + clip.post_seconds
            segments = segments_between(
                list_segments(self.settings.ring_dir / str(job.camera_id)), start, end
            )
            attributes: dict[str, object] = {"duration_s": round(job.ended_at - job.started_at, 1)}
            try:
                build_clip(self.settings.ffmpeg, segments, start, end, path)
            except FfmpegError as exc:
                # Every event must have a clip; without footage, use the snapshot.
                log.warning("event %s: no clip from ring buffer (%s)", job.event_id, exc)
                if job.snapshot is None:
                    self.outbox.mark_dropped(job.event_id, "no footage and no snapshot")
                    continue
                try:
                    still_clip(self.settings.ffmpeg, job.snapshot, path)
                except FfmpegError:
                    log.exception("event %s: fallback clip failed; retrying later", job.event_id)
                    self.outbox.postpone_clip(job.event_id, now + 300)
                    continue
                attributes["clip_fallback"] = "snapshot"
            self.outbox.close_event(job.event_id, path, attributes, time.time())
            self._sync_wanted.set()
            closed += 1
        return closed

    def sync_once(self, now: float) -> int:
        """Send events, then sightings and counts. Returns how many events were accepted."""
        accepted = self.syncer.sync_once(now)
        self.syncer.sync_sightings(now)
        self.syncer.sync_counts(now)
        return accepted

    # --- health -----------------------------------------------------------------------

    def heartbeat_once(self, now: float) -> None:
        applied = self.config.version if self.config else 0
        heartbeat = Heartbeat(
            sent_at=datetime.fromtimestamp(now, timezone.utc),
            software_version=__version__,
            config_version_applied=applied,
            uptime_seconds=int(time.monotonic() - self._started),
            cameras=[w.status(now) for w in list(self.workers.values())],
            system=system.collect(self.settings.data_dir, self.outbox.pending_count()),
        )
        response = self.client.heartbeat(heartbeat)
        drift = response.server_time.timestamp() - time.time()
        if abs(drift) > MAX_CLOCK_DRIFT_SECONDS:
            log.warning("clock differs from the cloud by %.1fs; check NTP", drift)
        if response.latest_config_version > applied:
            self._config_wanted.set()
            # The cloud also bumps the version when enrolled faces change.
            self._templates_wanted.set()
        if response.rotate_token:
            self.rotate_token()

    def rotate_token(self) -> None:
        """Swap the device token for a new one (plan 7.1). The new token is saved before
        it is used; if saving fails, the old token is used again within the cloud's grace
        period and the next heartbeat retries."""
        if self.tokens is None or not self.tokens.writable():
            log.warning("device token is due for rotation but can't be saved; skipping")
            return
        token = self.client.new_token()
        try:
            self.tokens.save(token)
        except OSError:
            log.exception("saving the new device token failed")
            return
        self.client.use_token(token)
        log.info("device token rotated")

    def remote_once(self, now: float) -> None:
        """Wait for one request from the cloud and hand it to a worker thread."""
        polled_at = time.monotonic()
        try:
            request = self.client.next_remote_request(self.settings.remote_wait_seconds)
        except CloudError as exc:
            # Back off so a cloud outage doesn't turn the long poll into a busy loop.
            self._remote_backoff = min(max(self._remote_backoff * 2, 1), 60)
            log.warning("remote: %s (retry in %.0fs)", exc, self._remote_backoff)
            self._stop.wait(self._remote_backoff)
            return
        self._remote_backoff = 0.0
        if request is not None:
            self.remote.submit(request)
        elif time.monotonic() - polled_at < 1:
            self._stop.wait(1)  # a cloud that doesn't hold the poll open
        self.remote.cleanup(now)

    def housekeeping(self, now: float) -> None:
        sent_before = now - self.settings.keep_sent_media_hours * 3600
        self.outbox.cleanup(self.settings.disk_quota_bytes, sent_before)
        self.outbox.cleanup_aggregates(now)
        self.settings.alive_path.touch()

    # --- lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        for directory in (self.settings.media_dir, self.settings.ring_dir):
            directory.mkdir(parents=True, exist_ok=True)
        recovered = self.outbox.recover_unfinished(time.time())
        if recovered:
            log.warning("%d events were open at the last shutdown; closing them", recovered)
        stored = self.store.load()
        if stored is not None:
            self.apply_config(stored)
        else:
            log.warning("no config yet; waiting for the cloud")
        s = self.settings
        self._loop("sync", s.sync_interval_seconds, self.sync_once, self._sync_wanted)
        self._loop("clips", 1, self.run_clip_jobs)
        self._loop(
            "config", s.config_poll_seconds, lambda _now: self.poll_config(), self._config_wanted
        )
        self._loop("heartbeat", s.heartbeat_seconds, self.heartbeat_once)
        self._loop("housekeeping", 10, self.housekeeping)
        # The long poll itself is the wait; a step lasts at most remote_wait_seconds + 15 s.
        self._loop("remote", 0, self.remote_once)
        if self.face_analyzer is not None:
            self._loop(
                "face-templates",
                s.face_templates_seconds,
                self.refresh_face_templates,
                self._templates_wanted,
            )

    def _loop(
        self,
        name: str,
        interval: float,
        step: Callable[[float], object],
        wake: threading.Event | None = None,
    ) -> None:
        def run() -> None:
            while not self._stop.is_set():
                self._ticks[name] = (time.monotonic(), interval)
                try:
                    step(time.time())
                except CloudError as exc:
                    log.warning("%s: %s", name, exc)
                except Exception:
                    log.exception("%s failed", name)
                if wake is None:
                    self._stop.wait(interval)
                else:
                    wake.wait(interval)
                    wake.clear()

        self._ticks[name] = (time.monotonic(), interval)
        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stalled_loops(self, now: float | None = None) -> list[str]:
        """Loops stuck in one step; the process should exit so Docker restarts it."""
        now = time.monotonic() if now is None else now
        return [
            name
            for name, (tick, interval) in self._ticks.items()
            if now - tick > LOOP_STALL_SECONDS + interval
        ]

    def shutdown(self) -> None:
        """Stop cameras (closing open events), then the loops. Clips for the closed events
        are cut after the next start from the ring buffer left on disk."""
        with self._apply_lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()
        self._stop.set()
        self._sync_wanted.set()
        self._config_wanted.set()
        self._templates_wanted.set()
        for thread in self._threads:
            thread.join(timeout=15)
        self.remote.close()
        self.remote.go2rtc.close()
