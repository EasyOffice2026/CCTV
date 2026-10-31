"""Requests from the cloud: live view, playback, recordings, frames, face embeddings."""

import base64
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import pytest

from cctv_contracts.v1 import (
    FaceEmbeddingRequest,
    FrameRequest,
    LiveRequest,
    NvrRef,
    PlaybackRequest,
    RecordingsRequest,
    RemoteRequest,
)
from cctv_edge.credentials import Credential
from cctv_edge.go2rtc import Go2rtc
from cctv_edge.nvr import playback_url
from cctv_edge.outbox import Outbox
from cctv_edge.remote import PLAYBACK_STREAM_TTL_SECONDS, RemoteWorker
from cctv_edge.service import EdgeService

from .conftest import camera, edge_config

USERNAME = "ai-reader"
PASSWORD = "S3cr3t!pw"
CREDENTIALS = {"nvr-main": Credential(USERNAME, PASSWORD)}
OFFER = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"
# 09:00-15:00 in Kuwait (UTC+3), the config's time zone.
START = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class FakeGo2rtc:
    def __init__(self) -> None:
        self.streams: dict[str, str] = {}
        self.offers: list[tuple[str, str]] = []
        self.read_only_config = False
        self.webrtc_error: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path == "/api/streams":
            if request.method == "PUT":
                self.streams[params["name"]] = params["src"]
                if self.read_only_config:
                    return httpx.Response(
                        400, text="open /config/go2rtc.yaml: read-only file system"
                    )
                return httpx.Response(200)
            if request.method == "GET":
                return httpx.Response(200 if params["src"] in self.streams else 404)
            if request.method == "DELETE":
                self.streams.pop(params["src"], None)
                return httpx.Response(200)
        if request.url.path == "/api/webrtc" and request.method == "POST":
            assert request.headers["content-type"] == "application/sdp"
            if params["src"] not in self.streams:
                return httpx.Response(404, text="stream not found")
            if self.webrtc_error:
                return httpx.Response(500, text=self.webrtc_error)
            self.offers.append((params["src"], request.content.decode()))
            return httpx.Response(201, text="v=0\r\no=- answer\r\n")
        return httpx.Response(404)


def _item(start: str, end: str) -> str:
    return (
        "<searchMatchItem><sourceID>{0}</sourceID><trackID>301</trackID>"
        f"<timeSpan><startTime>{start}</startTime><endTime>{end}</endTime></timeSpan>"
        "<metadataMatches><metadataDescriptor>recordType.meta.hikvision.com/timing"
        "</metadataDescriptor></metadataMatches></searchMatchItem>"
    )


class FakeNvr:
    """Hikvision ISAPI search with digest auth and two pages; times are NVR-local."""

    pages = (
        (
            "MORE",
            [
                _item("2026-10-31T08:00:00Z", "2026-10-31T10:00:00Z"),
                _item("2026-10-31T10:00:00Z", "2026-10-31T12:00:00Z"),
            ],
        ),
        ("OK", [_item("2026-10-31T12:30:00Z", "2026-10-31T14:00:00Z")]),
    )

    def __init__(self) -> None:
        self.bodies: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ISAPI/ContentMgmt/search"
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Digest "):
            challenge = 'Digest realm="NVR", nonce="abc123", qop="auth"'
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        assert f'username="{USERNAME}"' in auth
        assert PASSWORD not in auth
        body = request.content.decode()
        self.bodies.append(body)
        position = int(re.search(r"<searchResultPostion>(\d+)<", body).group(1))
        status, items = self.pages[0 if position == 0 else 1]
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<CMSearchResult version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            "<searchID>x</searchID><responseStatus>true</responseStatus>"
            f"<responseStatusStrg>{status}</responseStatusStrg>"
            f"<numOfMatches>{len(items)}</numOfMatches>"
            f"<matchList>{''.join(items)}</matchList></CMSearchResult>"
        )
        return httpx.Response(200, text=xml, headers={"Content-Type": "application/xml"})


@pytest.fixture
def go2rtc_fake() -> FakeGo2rtc:
    return FakeGo2rtc()


@pytest.fixture
def go2rtc(go2rtc_fake):
    client = Go2rtc("http://go2rtc.test", transport=httpx.MockTransport(go2rtc_fake.handler))
    yield client
    client.close()


@pytest.fixture
def nvr_fake() -> FakeNvr:
    return FakeNvr()


@pytest.fixture
def cams():
    with_nvr = camera(
        name="kitchen",
        credentials_ref="nvr-main",
        nvr=NvrRef(host="192.168.1.64", channel=3, credentials_ref="nvr-main"),
    )
    without_nvr = camera(name="dining", credentials_ref="nvr-main")
    return with_nvr, without_nvr


@pytest.fixture
def worker(client, go2rtc, nvr_fake, cams):
    config = edge_config(list(cams))
    w = RemoteWorker(
        client,
        go2rtc,
        lambda: config,
        CREDENTIALS,
        "ffmpeg",
        nvr_transport=httpx.MockTransport(nvr_fake.handler),
    )
    yield w
    w.close()


def remote(payload, expires_in: float = 30) -> RemoteRequest:
    return RemoteRequest(
        id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        request=payload,
    )


def assert_no_credentials(cloud) -> None:
    sent = json.dumps(cloud.remote_replies)
    for secret in (PASSWORD, quote(PASSWORD, safe=""), USERNAME):
        assert secret not in sent


def test_live_view_answers_with_go2rtc_sdp(worker, cloud, cams, go2rtc_fake):
    cam = cams[0]
    request = remote(LiveRequest(camera_id=cam.id, sdp_offer=OFFER))
    reply = worker.process(request)

    assert reply.ok and reply.sdp_answer.startswith("v=0")
    assert cloud.remote_replies[str(request.id)]["sdp_answer"] == reply.sdp_answer
    name = f"live-{cam.id}"
    assert go2rtc_fake.offers == [(name, OFFER)]
    # The sub-stream, with the local credentials added only inside the box.
    assert go2rtc_fake.streams[name] == (
        f"rtsp://{USERNAME}:{quote(PASSWORD, safe='')}@nvr.local:554/Streaming/Channels/102"
    )
    assert_no_credentials(cloud)


def test_read_only_go2rtc_config_still_works(worker, cams, go2rtc_fake):
    go2rtc_fake.read_only_config = True
    reply = worker.process(remote(LiveRequest(camera_id=cams[0].id, sdp_offer=OFFER)))
    assert reply.ok


def test_playback_uses_nvr_local_time_and_is_cleaned_up(worker, cloud, cams, go2rtc_fake):
    cam = cams[0]
    request = remote(
        PlaybackRequest(
            camera_id=cam.id, start=START, end=START + timedelta(minutes=30), sdp_offer=OFFER
        )
    )
    now = time.time()
    reply = worker.process(request, now)
    assert reply.ok

    name = f"pb-{request.id}"
    assert go2rtc_fake.streams[name] == (
        f"rtsp://{USERNAME}:{quote(PASSWORD, safe='')}@192.168.1.64:554/Streaming/tracks/301"
        "?starttime=20261031T090000Z&endtime=20261031T093000Z"
    )
    assert worker.playback_streams == [name]
    assert_no_credentials(cloud)

    worker.cleanup(now + 60)
    assert name in go2rtc_fake.streams
    worker.cleanup(now + PLAYBACK_STREAM_TTL_SECONDS + 1)
    assert name not in go2rtc_fake.streams
    assert worker.playback_streams == []


def test_playback_url_without_credentials():
    nvr = NvrRef(host="nvr.local", rtsp_port=8554, channel=12)
    url = playback_url(nvr, START, START + timedelta(hours=1), "Asia/Kuwait")
    assert url == (
        "rtsp://nvr.local:8554/Streaming/tracks/1201"
        "?starttime=20261031T090000Z&endtime=20261031T100000Z"
    )


def test_recordings_from_isapi_search(worker, cloud, cams, nvr_fake):
    request = remote(RecordingsRequest(camera_id=cams[0].id, start=START, end=END))
    reply = worker.process(request)

    assert reply.ok
    spans = [(s.start, s.end) for s in reply.recordings]
    utc = timezone.utc
    assert spans == [
        # 08:00-10:00 local is 05:00-07:00 UTC, clipped to the requested start.
        (START, datetime(2026, 10, 31, 7, 0, tzinfo=utc)),
        (datetime(2026, 10, 31, 7, 0, tzinfo=utc), datetime(2026, 10, 31, 9, 0, tzinfo=utc)),
        (datetime(2026, 10, 31, 9, 30, tzinfo=utc), datetime(2026, 10, 31, 11, 0, tzinfo=utc)),
    ]
    assert len(nvr_fake.bodies) == 2  # followed the MORE page
    assert "<trackID>301</trackID>" in nvr_fake.bodies[0]
    assert "<startTime>2026-10-31T09:00:00Z</startTime>" in nvr_fake.bodies[0]
    assert "<searchResultPostion>2</searchResultPostion>" in nvr_fake.bodies[1]
    assert_no_credentials(cloud)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (lambda cams: LiveRequest(camera_id=uuid.uuid4(), sdp_offer=OFFER), "not configured"),
        (
            lambda cams: PlaybackRequest(
                camera_id=cams[1].id, start=START, end=END - timedelta(hours=4), sdp_offer=OFFER
            ),
            "no NVR channel",
        ),
        (
            lambda cams: RecordingsRequest(camera_id=cams[1].id, start=START, end=END),
            "no NVR channel",
        ),
        (
            lambda cams: FaceEmbeddingRequest(staff_id=uuid.uuid4(), images_base64=["aGk="]),
            "face model not installed",
        ),
    ],
)
def test_requests_that_cannot_be_served_get_an_error_reply(worker, cloud, cams, payload, error):
    request = remote(payload(cams))
    reply = worker.process(request)
    assert not reply.ok and error in reply.error
    assert cloud.remote_replies[str(request.id)] == {"ok": False, "error": reply.error}


def test_expired_request_is_dropped(worker, cloud, cams, go2rtc_fake):
    request = remote(LiveRequest(camera_id=cams[0].id, sdp_offer=OFFER), expires_in=-1)
    assert worker.process(request) is None
    assert cloud.remote_replies == {}
    assert go2rtc_fake.streams == {}


def test_errors_never_carry_credentials(worker, cloud, cams, go2rtc_fake):
    go2rtc_fake.webrtc_error = (
        f"streams: rtsp://{USERNAME}:{PASSWORD}@nvr.local/102 refused; "
        f"src=rtsp%3A%2F%2F{quote(USERNAME, safe='')}%3A{quote(PASSWORD, safe='')}%40nvr"
    )
    reply = worker.process(remote(LiveRequest(camera_id=cams[0].id, sdp_offer=OFFER)))
    assert not reply.ok and "HTTP 500" in reply.error
    assert_no_credentials(cloud)


def test_missing_local_credentials_are_reported(client, go2rtc, cams):
    config = edge_config(list(cams))
    w = RemoteWorker(client, go2rtc, lambda: config, {}, "ffmpeg")
    try:
        reply = w.process(remote(LiveRequest(camera_id=cams[0].id, sdp_offer=OFFER)))
    finally:
        w.close()
    assert not reply.ok and "no local credentials named 'nvr-main'" in reply.error


def test_frame_is_grabbed_from_the_sub_stream(worker, cloud, cams, monkeypatch):
    urls = []

    def fake_grab(url: str) -> bytes:
        urls.append(url)
        return b"\xff\xd8 jpeg"

    monkeypatch.setattr(worker, "grab_frame", fake_grab)
    reply = worker.process(remote(FrameRequest(camera_id=cams[0].id)))
    assert reply.ok and base64.b64decode(reply.jpeg_base64) == b"\xff\xd8 jpeg"
    assert urls[0].endswith("@nvr.local:554/Streaming/Channels/102")
    assert_no_credentials(cloud)


def test_grab_frame_with_ffmpeg(worker, ffmpeg_bin, sample_video):
    worker.ffmpeg_bin = ffmpeg_bin
    jpeg = worker.grab_frame(str(sample_video))
    assert jpeg[:2] == b"\xff\xd8"


def test_cloud_client_long_poll(client, cloud):
    assert client.next_remote_request(1) is None

    request = remote(FrameRequest(camera_id=uuid.uuid4()))
    cloud.remote_queue.append(json.loads(request.model_dump_json()))
    assert client.next_remote_request(1) == request

    # A kind this box doesn't know: answered as unsupported, not dropped silently.
    unknown_id = str(uuid.uuid4())
    cloud.remote_queue.append(
        {"id": unknown_id, "expires_at": request.expires_at.isoformat(), "request": {"kind": "ptz"}}
    )
    assert client.next_remote_request(1) is None
    assert cloud.remote_replies[unknown_id] == {"ok": False, "error": "unsupported request"}


def test_service_polls_and_answers(settings, client, cloud, go2rtc, cams):
    outbox = Outbox(settings.outbox_path)
    service = EdgeService(
        settings,
        client,
        outbox,
        credentials=CREDENTIALS,
        start_streams=False,
        go2rtc=go2rtc,
    )
    try:
        cloud.config = edge_config(list(cams))
        service.poll_config()
        request = remote(LiveRequest(camera_id=cams[0].id, sdp_offer=OFFER))
        cloud.remote_queue.append(json.loads(request.model_dump_json()))
        service.remote_once(time.time())
        deadline = time.monotonic() + 5
        while str(request.id) not in cloud.remote_replies and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cloud.remote_replies[str(request.id)]["ok"] is True
    finally:
        service.shutdown()
        outbox.close()
