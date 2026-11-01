"""Plan 9.1: the install-day site check (cameras, NVR clock and disks, cloud, box)."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import httpx

from cctv_contracts.v1 import NvrRef
from cctv_edge import sitecheck
from cctv_edge.cloud import CloudError
from cctv_edge.credentials import Credential
from cctv_edge.sitecheck import Finding, StreamInfo
from tests.conftest import camera, edge_config

KUWAIT = ZoneInfo("Asia/Kuwait")


def nvr_transport(clock: str, disks: str, seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/ISAPI/System/time":
            return httpx.Response(200, text=clock)
        if request.url.path == "/ISAPI/ContentMgmt/Storage":
            return httpx.Response(200, text=disks)
        if request.url.path == "/readyz":
            return httpx.Response(
                200, headers={"Date": format_datetime(datetime.now(timezone.utc), usegmt=True)}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def clock_xml(local: datetime, mode: str = "NTP") -> str:
    return (
        '<Time xmlns="http://www.hikvision.com/ver20/XMLSchema">'
        f"<timeMode>{mode}</timeMode><localTime>{local.isoformat(timespec='seconds')}</localTime>"
        "<timeZone>CST-3:00:00</timeZone></Time>"
    )


def disks_xml(*statuses: str) -> str:
    items = "".join(
        f"<hdd><id>{i}</id><capacity>3815447</capacity><status>{s}</status></hdd>"
        for i, s in enumerate(statuses, start=1)
    )
    return f'<storage xmlns="http://www.hikvision.com/ver20/XMLSchema"><hddList>{items}</hddList></storage>'


NVR = NvrRef(host="192.168.1.10", channel=1, credentials_ref="nvr")
CRED = Credential("ai-readonly", "secret")


def levels(findings: list[Finding]) -> dict[str, str]:
    return {f.message.split(":")[0]: f.level for f in findings}


def test_a_healthy_nvr():
    now = datetime.now(timezone.utc)
    seen: list[httpx.Request] = []
    found = sitecheck.check_nvr(
        NVR,
        CRED,
        "Asia/Kuwait",
        nvr_transport(clock_xml(now.astimezone(KUWAIT)), disks_xml("ok"), seen),
    )
    assert [f.level for f in found] == ["ok", "ok"], found
    assert found[1].message == "disk 1: ok, 3726 GB"
    assert {r.method for r in seen} == {"GET"}  # only reads


def test_the_problems_found_in_the_survey():
    """Egaila: "Disk Exception". Central kitchen: clock 5 h ahead. Ardiya: 4 min ahead."""
    now = datetime.now(timezone.utc)
    ahead = (now + timedelta(minutes=4)).astimezone(KUWAIT)
    found = sitecheck.check_nvr(
        NVR, CRED, "Asia/Kuwait", nvr_transport(clock_xml(ahead, "manual"), disks_xml("error"))
    )
    assert [f.level for f in found] == ["fail", "warn", "fail"], found
    assert found[0].message.startswith("clock vs box: +2")  # about +240 s
    # Wrong time zone: the overlay says 15:55 when it is 10:55 in Kuwait.
    wrong_zone = (now + timedelta(hours=5)).astimezone(timezone(timedelta(hours=8)))
    found = sitecheck.check_nvr(
        NVR, CRED, "Asia/Kuwait", nvr_transport(clock_xml(wrong_zone), disks_xml())
    )
    messages = [f.message for f in found if f.level == "fail"]
    assert any("time zone" in m for m in messages)
    assert "no disk: the NVR is not recording" in messages


def test_unreachable_or_refusing_nvr():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    found = sitecheck.check_nvr(NVR, CRED, "Asia/Kuwait", httpx.MockTransport(refuse))
    assert found == [Finding("fail", "NVR 192.168.1.10", "refused the credentials")]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    found = sitecheck.check_nvr(NVR, CRED, "Asia/Kuwait", httpx.MockTransport(down))
    assert found[0].level == "fail" and "unreachable" in found[0].message


def test_camera_streams_against_the_minimum_spec():
    probed: list[str] = []

    def probe(url: str) -> StreamInfo:
        probed.append(url)
        if "Channels/302" in url:
            raise RuntimeError("Connection refused")
        if "Channels/1" in url:
            return StreamInfo("h264", 1920, 1080, 25.0)
        return StreamInfo("mjpeg", 1280, 720, 8.0)

    cameras = [
        camera(name="Dining", credentials_ref="nvr"),
        camera(
            name="Kitchen",
            rtsp_main="rtsp://192.168.1.10:554/Streaming/Channels/201",
            rtsp_sub=None,
            credentials_ref="nvr",
        ),
        camera(
            name="Door",
            rtsp_main="rtsp://192.168.1.10:554/Streaming/Channels/301",
            rtsp_sub="rtsp://192.168.1.10:554/Streaming/Channels/302",
            credentials_ref="missing",
        ),
    ]
    found = sitecheck.check_cameras(cameras, {"nvr": CRED}, probe)
    summary = [(f.topic, f.level, f.message) for f in found]
    assert summary[0] == ("camera Dining", "ok", "main h264 1920x1080 25 fps")
    assert summary[1][1] == "ok"
    assert summary[2] == (
        "camera Kitchen",
        "warn",
        "main mjpeg 1280x720 8 fps; codec should be H.264 or H.265; below 1080p; below 15 fps",
    )
    assert summary[3][1] == "warn" and "no sub-stream" in summary[3][2]
    assert summary[4] == ("camera Door", "fail", "no credentials named 'missing'")
    # Credentials are added for ffprobe, never printed.
    assert probed[0].startswith("rtsp://ai-readonly:secret@")
    assert "secret" not in json.dumps([f.message for f in found])


class StubClient:
    def __init__(self, config=None, error: CloudError | None = None) -> None:
        self.config, self.error = config, error

    def fetch_config(self, version):
        if self.error:
            raise self.error
        return self.config


def test_full_run(settings):
    settings = settings.model_copy(update={"disk_quota_bytes": 1024})
    settings.data_dir.mkdir(parents=True)
    nvr = NvrRef(host="192.168.1.10", channel=1, credentials_ref="nvr")
    settings.credentials_file.parent.mkdir(parents=True, exist_ok=True)
    settings.credentials_file.write_text(json.dumps({"nvr": {"username": "u", "password": "p"}}))
    config = edge_config([camera(name="Dining", credentials_ref="nvr", nvr=nvr)])
    now = datetime.now(timezone.utc).astimezone(KUWAIT)
    transport = nvr_transport(clock_xml(now), disks_xml("ok", "ok"))
    found = sitecheck.run(
        settings,
        StubClient(config),
        probe=lambda url: StreamInfo("hevc", 2560, 1440, 20),
        ntp=lambda: True,
        transport=transport,
    )
    assert all(f.level == "ok" for f in found), sitecheck.report(found)
    topics = [f.topic for f in found]
    assert topics[:2] == ["clock", "disk"] and "pairing" in topics and "NVR 192.168.1.10" in topics

    # A box that was never paired, without NTP: failures, and the exit code says so.
    found = sitecheck.run(
        settings,
        StubClient(error=CloudError("unauthorized", 401)),
        probe=lambda url: StreamInfo("hevc", 2560, 1440, 20),
        ntp=lambda: False,
        transport=transport,
    )
    fails = {(f.topic, f.message) for f in found if f.level == "fail"}
    assert ("pairing", "device token refused (pair the box again)") in fails
    assert ("clock", "not NTP-synced: event times would be wrong") in fails
    assert ("config", "no config: cameras can't be checked") in fails
    assert "FAIL" in sitecheck.report(found)
    assert json.loads(sitecheck.report(found, as_json=True))[0]["topic"] == "clock"


def test_frame_rates():
    assert sitecheck._rate("25/1") == 25.0
    assert sitecheck._rate("30000/1001") == 29.97
    assert sitecheck._rate("0/0") == 0.0
    assert sitecheck._rate(None) == 0.0
