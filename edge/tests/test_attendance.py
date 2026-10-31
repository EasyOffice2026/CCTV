"""Attendance: template gallery, matching, sightings, enrolment and the cloud side of it.
A fake face analyzer stands in for the face models."""

import base64
import io
import time
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from PIL import Image

from cctv_contracts.enums import FeatureKey
from cctv_contracts.v1 import FaceEmbeddingRequest, FaceTemplate, FeatureConfig, RemoteRequest
from cctv_edge.attendance import AttendanceSettings, AttendanceWatcher
from cctv_edge.face import EnrolmentError, Face, FaceGallery, enrolment_embedding, normalise
from cctv_edge.geometry import Box
from cctv_edge.outbox import Outbox
from cctv_edge.pipeline import CameraPipeline
from cctv_edge.schedule import BranchSchedule
from cctv_edge.service import EdgeService
from cctv_edge.sync import Syncer

from .conftest import camera, edge_config

VERSION = "sface-2021dec"
DIM = 128
RNG = np.random.default_rng(7)
ALICE, BOB = uuid.uuid4(), uuid.uuid4()
ALICE_VEC, BOB_VEC = normalise(RNG.normal(size=DIM)), normalise(RNG.normal(size=DIM))
FRAME = np.zeros((72, 128, 3), np.uint8)


def near(vector, noise=0.1):
    return normalise(vector + RNG.normal(scale=noise / np.sqrt(DIM), size=DIM))


def face(vector, size=100) -> Face:
    return Face(Box(0.4, 0.2, 0.6, 0.6), 0.95, size, np.asarray(vector, np.float32))


class FakeAnalyzer:
    model_version = VERSION

    def __init__(self, faces=()):
        self.faces = list(faces)  # returned for every frame / photo, in turn if a list of lists
        self.calls = 0
        self.qualities = []

    def analyze(self, frame, quality):
        self.calls += 1
        self.qualities.append(quality)
        if self.faces and isinstance(self.faces[0], list):
            return self.faces[(self.calls - 1) % len(self.faces)]
        return list(self.faces)


def template(staff_id, vector, version=VERSION) -> FaceTemplate:
    return FaceTemplate(staff_id=staff_id, model_version=version, embedding=vector.tolist())


@pytest.fixture
def gallery():
    g = FaceGallery()
    g.replace([template(ALICE, ALICE_VEC), template(BOB, BOB_VEC)], VERSION)
    return g


# --- gallery and matching ---------------------------------------------------------------


def test_gallery_matches_the_right_person(gallery):
    staff, similarity = gallery.match(near(ALICE_VEC), 0.4)
    assert staff == ALICE and similarity > 0.9
    assert gallery.match(near(BOB_VEC), 0.4)[0] == BOB
    assert gallery.match(normalise(RNG.normal(size=DIM)), 0.4) is None  # a customer


def test_gallery_ignores_other_model_versions():
    g = FaceGallery()
    count = g.replace(
        [template(ALICE, ALICE_VEC), template(BOB, BOB_VEC, version="arcface-1")], VERSION
    )
    assert count == 1 and len(g) == 1
    assert g.match(near(BOB_VEC), 0.4) is None
    assert g.match(np.ones(64, np.float32), 0.0) is None  # wrong length: no match


def test_watcher_sends_one_sighting_per_interval(gallery):
    analyzer = FakeAnalyzer([face(near(ALICE_VEC)), face(normalise(RNG.normal(size=DIM)))])
    cam = uuid.uuid4()
    watcher = AttendanceWatcher(cam, analyzer, gallery, AttendanceSettings())
    sightings = []
    for i in range(10 * 5):  # 10 s at 5 fps, then again after the interval
        sightings += watcher.process(FRAME, 1000 + i / 5)
    sightings += watcher.process(FRAME, 1061)
    assert [s.staff_id for s in sightings] == [ALICE, ALICE]
    first = sightings[0]
    assert first.camera_id == cam and 0.9 < first.similarity <= 1
    assert first.seen_at == datetime.fromtimestamp(1000, timezone.utc)
    # The unknown face left no trace: only matched staff ids are remembered.
    assert set(watcher._last_sent) == {ALICE}


def test_watcher_idles_without_templates():
    analyzer = FakeAnalyzer([face(ALICE_VEC)])
    watcher = AttendanceWatcher(uuid.uuid4(), analyzer, FaceGallery(), AttendanceSettings())
    assert watcher.process(FRAME, 0) == []
    assert analyzer.calls == 0  # no faces are even looked at


def test_attendance_settings_from_params(gallery):
    analyzer = FakeAnalyzer()
    feature = FeatureConfig(
        key=FeatureKey.ATTENDANCE, params={"min_face_px": 90, "match_threshold": 0.5}
    )
    pipeline = CameraPipeline(
        camera([feature]),
        BranchSchedule([], "Asia/Kuwait"),
        {},
        face_analyzer=analyzer,
        gallery=gallery,
    )
    assert pipeline.active and pipeline.needs_detail and pipeline.detectors == []
    pipeline.process(FRAME, 0)
    assert analyzer.qualities[0].min_size_px == 90
    assert pipeline.attendance.settings.match_threshold == 0.5
    no_model = CameraPipeline(camera([feature]), BranchSchedule([], "Asia/Kuwait"), {})
    assert not no_model.active and "face" in no_model.missing_labels


# --- enrolment --------------------------------------------------------------------------


def photo(fmt="JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 90, 60)).save(buffer, fmt)
    return buffer.getvalue()


def test_enrolment_averages_the_photos():
    a, b, c = near(ALICE_VEC, 0.3), near(ALICE_VEC, 0.3), near(ALICE_VEC, 0.3)
    analyzer = FakeAnalyzer([[face(a)], [face(b), face(BOB_VEC, size=40)], [face(c)]])
    result = enrolment_embedding(analyzer, [photo(), photo("PNG"), photo()])
    assert result.shape == (DIM,) and float(np.linalg.norm(result)) == pytest.approx(1.0)
    assert float(result @ ALICE_VEC) > float(a @ ALICE_VEC) - 0.05
    assert float(result @ normalise(a + b + c)) == pytest.approx(1.0, abs=1e-5)


@pytest.mark.parametrize(
    ("faces", "photos", "error"),
    [
        ([[]], [photo()], "no clear face"),
        ([[face(ALICE_VEC, 100), face(BOB_VEC, 90)]], [photo()], "more than one face"),
        ([[face(ALICE_VEC)], [face(BOB_VEC)]], [photo(), photo()], "same person"),
        ([[face(ALICE_VEC)]], [b"not an image"], "not a readable image"),
    ],
)
def test_enrolment_refusals(faces, photos, error):
    with pytest.raises(EnrolmentError, match=error):
        enrolment_embedding(FakeAnalyzer(faces), photos)


def test_enrolment_rejects_other_image_formats():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, "GIF")
    with pytest.raises(EnrolmentError, match="JPEG or PNG"):
        enrolment_embedding(FakeAnalyzer([[face(ALICE_VEC)]]), [buffer.getvalue()])


def test_remote_face_embedding_request(settings, client, cloud, tmp_path):
    analyzer = FakeAnalyzer([face(near(ALICE_VEC))])
    outbox = Outbox(settings.outbox_path)
    service = EdgeService(settings, client, outbox, start_streams=False, face_analyzer=analyzer)
    try:
        request = RemoteRequest(
            id=uuid.uuid4(),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            request=FaceEmbeddingRequest(
                staff_id=ALICE, images_base64=[base64.b64encode(photo()).decode()] * 2
            ),
        )
        reply = service.remote.process(request)
        assert reply.ok and reply.model_version == VERSION and len(reply.embedding) == DIM
        assert cloud.remote_replies[str(request.id)]["model_version"] == VERSION
        assert list(tmp_path.rglob("*.jpg")) == []  # photos never touch the disk

        analyzer.faces = []
        reply = service.remote.process(request.model_copy(update={"id": uuid.uuid4()}))
        assert not reply.ok and "photo 1: no clear face" in reply.error
    finally:
        service.shutdown()
        outbox.close()


# --- the cloud side ---------------------------------------------------------------------


def test_templates_are_fetched_with_etag_and_kept_in_memory(settings, client, cloud):
    cloud.templates = [
        template(ALICE, ALICE_VEC).model_dump(mode="json"),
        template(BOB, BOB_VEC, version="old-model").model_dump(mode="json"),
    ]
    outbox = Outbox(settings.outbox_path)
    service = EdgeService(
        settings, client, outbox, start_streams=False, face_analyzer=FakeAnalyzer()
    )
    try:
        service.refresh_face_templates(time.time())
        assert len(service.gallery) == 1
        service.refresh_face_templates(time.time())  # unchanged: 304
        assert cloud.template_requests == [None, '"t1"']
        assert len(service.gallery) == 1
        files = [p.name for p in settings.data_dir.rglob("*") if p.is_file()]
        assert all("template" not in name and "face" not in name for name in files)

        # A newer config version (the cloud bumps it when enrolments change) also
        # wakes the template loop.
        cloud.config = edge_config([camera([FeatureKey.ATTENDANCE])], version=3)
        service.heartbeat_once(time.time())
        assert service._templates_wanted.is_set()
    finally:
        service.shutdown()
        outbox.close()


def test_no_face_model_means_no_template_requests(settings, client, cloud):
    outbox = Outbox(settings.outbox_path)
    service = EdgeService(settings, client, outbox, start_streams=False)
    try:
        assert service.face_analyzer is None
        service.refresh_face_templates(time.time())
        assert cloud.template_requests == []
    finally:
        service.shutdown()
        outbox.close()


def test_sightings_sync_and_rejected_ones_are_not_resent(tmp_path, client, cloud, gallery):
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    syncer = Syncer(outbox, client)
    analyzer = FakeAnalyzer([face(near(ALICE_VEC)), face(near(BOB_VEC))])
    watcher = AttendanceWatcher(uuid.uuid4(), analyzer, gallery, AttendanceSettings())
    outbox.add_sightings(watcher.process(FRAME, 1000.0), 1000.0)

    cloud.down = True
    assert syncer.sync_sightings(1000) == 0
    assert outbox.due_sightings(1000, 10) == []  # backing off
    cloud.down = False
    cloud.reject_staff = {str(BOB)}  # e.g. withdrew consent
    assert syncer.sync_sightings(10_000) == 1
    assert [s["staff_id"] for s in cloud.sightings] == [str(ALICE)]
    assert outbox.due_sightings(20_000, 10) == []  # Bob's sighting was not kept
    outbox.close()


def test_service_stores_sightings_from_the_pipeline(settings, client, cloud, gallery):
    outbox = Outbox(settings.outbox_path)
    service = EdgeService(
        settings,
        client,
        outbox,
        start_streams=False,
        face_analyzer=FakeAnalyzer([face(near(ALICE_VEC))]),
    )
    try:
        service.gallery.replace([template(ALICE, ALICE_VEC)], VERSION)
        cam = camera([FeatureKey.ATTENDANCE])
        cloud.config = edge_config([cam])
        service.poll_config()
        worker = service.workers[cam.id]
        assert worker.detail and service.analysis_size(True) == (1280, 720)
        worker.process(FRAME, time.time())
        service.sync_once(time.time())
        assert [s["staff_id"] for s in cloud.sightings] == [str(ALICE)]
    finally:
        service.shutdown()
        outbox.close()


def test_opencv_analyzer_reports_bad_model_files(tmp_path):
    try:
        import cv2  # noqa: F401
    except ImportError:
        pytest.skip("OpenCV not installed")
    from cctv_edge.face import OpenCvFaceAnalyzer
    from cctv_edge.models.manifest import ModelError, ModelManifest

    junk = tmp_path / "junk.onnx"
    junk.write_bytes(b"not a model")

    def manifest(task):
        return ModelManifest(
            name=task, version="1", task=task, runtime="opencv", sha256="0" * 64, licence="MIT"
        )

    with pytest.raises(ModelError, match="cannot load"):
        OpenCvFaceAnalyzer((manifest("face_detector"), junk), (manifest("face_embedder"), junk))
