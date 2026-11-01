"""Site check for the install day (plan 9.1, 9.2): `python -m cctv_edge --check`.

Checks what the plan's site survey found going wrong (§0): cameras that don't stream,
NVR clocks minutes or hours off, an NVR disk in "exception" (nothing recorded), plus the
box itself: device token, cloud connection, clock, disk and temperature. Prints one line
per finding and exits with 1 if anything failed; `--json` for the install log.

Nothing here changes the cameras or the NVR; it only reads (RTSP, ISAPI GET).
"""

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from cctv_contracts.v1 import CameraConfig, EdgeConfig, NvrRef
from cctv_edge import ffmpeg
from cctv_edge.cloud import CloudClient, CloudError
from cctv_edge.config_store import ConfigStore
from cctv_edge.credentials import Credential, load_credentials, redact, with_credentials
from cctv_edge.settings import EdgeSettings
from cctv_edge.system import max_temperature_c

Level = Literal["ok", "warn", "fail"]

# The plan's minimum camera spec (11.3): 1080p, 15 fps, H.264 or H.265.
MIN_WIDTH, MIN_HEIGHT, MIN_FPS = 1920, 1080, 15
CODECS = {"h264", "hevc"}
# Clock drift: events are stamped by the box, but playback asks the NVR by its own time.
WARN_DRIFT = timedelta(seconds=5)
FAIL_DRIFT = timedelta(seconds=60)
# Hikvision HDD states that record.
DISK_OK = {"ok", "idle", "sleep", "sleeping"}
MAX_XML_BYTES = 256 * 1024


@dataclass(frozen=True)
class Finding:
    level: Level
    topic: str
    message: str


@dataclass(frozen=True)
class StreamInfo:
    codec: str
    width: int
    height: int
    fps: float


Prober = Callable[[str], StreamInfo]


def ffprobe(binary: str, timeout: float = 20) -> Prober:
    def probe(url: str) -> StreamInfo:
        args = [
            binary,
            "-v",
            "error",
            *ffmpeg.input_args(url)[:-2],
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            url,
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed binary, no shell
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("no answer (timeout)") from None
        except OSError as exc:
            raise RuntimeError(f"cannot run {binary}: {exc}") from None
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            raise RuntimeError(redact(lines[-1] if lines else "failed"))
        streams = json.loads(result.stdout or "{}").get("streams") or []
        if not streams:
            raise RuntimeError("no video stream")
        s = streams[0]
        return StreamInfo(
            codec=s.get("codec_name", "?"),
            width=int(s.get("width") or 0),
            height=int(s.get("height") or 0),
            fps=_rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")),
        )

    return probe


def _rate(value: str | None) -> float:
    try:
        num, _, den = (value or "0/0").partition("/")
        return round(float(num) / float(den or 1), 2) if float(den or 1) else 0.0
    except ValueError:
        return 0.0


# --- the box ----------------------------------------------------------------------------


def check_box(settings: EdgeSettings, ntp: Callable[[], bool | None]) -> list[Finding]:
    found = []
    synced = ntp()
    if synced is None:
        found.append(Finding("warn", "clock", "can't tell whether the clock is NTP-synced"))
    elif synced:
        found.append(Finding("ok", "clock", "NTP-synced"))
    else:
        found.append(Finding("fail", "clock", "not NTP-synced: event times would be wrong"))
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError as exc:
        found.append(Finding("fail", "disk", f"{settings.data_dir}: {exc}"))
    else:
        gb = free / 1024**3
        level: Level = "ok" if free >= 2 * settings.disk_quota_bytes else "warn"
        if free < settings.disk_quota_bytes:
            level = "fail"
        found.append(Finding(level, "disk", f"{gb:.0f} GB free in {settings.data_dir}"))
    temperature = max_temperature_c()
    if temperature is not None:
        level = "ok" if temperature < 80 else "warn" if temperature < 90 else "fail"
        found.append(Finding(level, "temperature", f"{temperature} °C"))
    return found


def ntp_synced() -> bool | None:
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"yes": True, "no": False}.get(out)


def check_cloud(
    settings: EdgeSettings, client: CloudClient, transport: httpx.BaseTransport | None = None
) -> tuple[list[Finding], EdgeConfig | None]:
    """Reachable, clock in line with the cloud's, device token accepted (by fetching the
    config, which is also what the camera checks need)."""
    found = []
    url = settings.cloud_url.rstrip("/") + "/readyz"
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds, transport=transport) as http:
            response = http.get(url)
    except httpx.HTTPError as exc:
        return [Finding("fail", "cloud", f"{url} unreachable: {type(exc).__name__}")], None
    if response.status_code != 200:
        found.append(Finding("fail", "cloud", f"{url}: HTTP {response.status_code}"))
    else:
        found.append(Finding("ok", "cloud", f"{url} answers"))
    if "date" in response.headers:
        drift = datetime.now(timezone.utc) - parsedate_to_datetime(response.headers["date"])
        found.append(_drift("clock", "box vs cloud", drift))
    config = None
    try:
        config = client.fetch_config(None)
    except CloudError as exc:
        reason = "device token refused (pair the box again)" if exc.status == 401 else str(exc)
        found.append(Finding("fail", "pairing", reason))
    else:
        found.append(Finding("ok", "pairing", "device token accepted"))
    return found, config


def _drift(topic: str, what: str, drift: timedelta) -> Finding:
    size = abs(drift)
    text = f"{what}: {drift.total_seconds():+.0f} s"
    if size >= FAIL_DRIFT:
        return Finding("fail", topic, text)
    return Finding("warn" if size >= WARN_DRIFT else "ok", topic, text)


# --- cameras ----------------------------------------------------------------------------


def check_cameras(
    cameras: Iterable[CameraConfig], credentials: dict[str, Credential], probe: Prober
) -> list[Finding]:
    found = []
    for camera in cameras:
        topic = f"camera {camera.name}"
        credential = credentials.get(camera.credentials_ref) if camera.credentials_ref else None
        if camera.credentials_ref and credential is None:
            found.append(Finding("fail", topic, f"no credentials named {camera.credentials_ref!r}"))
            continue
        for kind, url in (("main", camera.rtsp_main), ("sub", camera.rtsp_sub)):
            if url is None:
                found.append(
                    Finding("warn", topic, "no sub-stream: the AI decodes the main stream")
                )
                continue
            try:
                info = probe(with_credentials(url, credential))
            except (RuntimeError, ValueError) as exc:
                found.append(Finding("fail", topic, f"{kind} stream: {exc}"))
                continue
            text = f"{kind} {info.codec} {info.width}x{info.height} {info.fps:g} fps"
            problems = []
            if info.codec not in CODECS:
                problems.append("codec should be H.264 or H.265")
            if kind == "main" and (info.width < MIN_WIDTH or info.height < MIN_HEIGHT):
                problems.append("below 1080p")
            if kind == "main" and 0 < info.fps < MIN_FPS:
                problems.append(f"below {MIN_FPS} fps")
            level: Level = "warn" if problems else "ok"
            found.append(Finding(level, topic, "; ".join([text, *problems])))
    return found


# --- NVRs (Hikvision ISAPI) -------------------------------------------------------------


def _xml(response: httpx.Response) -> ElementTree.Element:
    if len(response.content) > MAX_XML_BYTES:
        raise ValueError("response too large")
    # From the NVR on the branch LAN, size-capped; expat refuses entity-expansion bombs.
    root = ElementTree.fromstring(response.content)  # noqa: S314
    for element in root.iter():
        element.tag = re.sub(r"^\{[^}]*\}", "", element.tag)
    return root


def check_nvr(
    nvr: NvrRef,
    credential: Credential | None,
    branch_tz: str,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> list[Finding]:
    topic = f"NVR {nvr.host}"
    auth = httpx.DigestAuth(credential.username, credential.password) if credential else None
    base = f"http://{nvr.host}:{nvr.http_port}/ISAPI"
    found = []
    try:
        with httpx.Client(auth=auth, timeout=10, transport=transport) as http:
            time_reply = http.get(f"{base}/System/time")
            storage_reply = http.get(f"{base}/ContentMgmt/Storage")
    except httpx.HTTPError as exc:
        return [Finding("fail", topic, f"unreachable: {type(exc).__name__}")]
    if 401 in (time_reply.status_code, storage_reply.status_code):
        return [Finding("fail", topic, "refused the credentials")]

    # Clock: the survey found NVRs 4 minutes and 5 hours off.
    try:
        clock = _xml(time_reply)
        local = datetime.fromisoformat((clock.findtext("localTime") or "").strip())
        mode = (clock.findtext("timeMode") or "").strip()
    except (ElementTree.ParseError, ValueError):
        found.append(Finding("warn", topic, "can't read the NVR's time"))
    else:
        tz = ZoneInfo(branch_tz)
        aware = local if local.tzinfo else local.replace(tzinfo=tz)
        found.append(_drift(topic, "clock vs box", aware - (now or datetime.now(timezone.utc))))
        expected = datetime.now(tz).utcoffset()
        if local.tzinfo and local.utcoffset() != expected:
            found.append(Finding("fail", topic, f"time zone {local.tzinfo} is not {branch_tz}"))
        if mode and mode.upper() != "NTP":
            found.append(Finding("warn", topic, f"time set {mode}, not by NTP"))

    # Disks: "Disk Exception" on site meant nothing was being recorded.
    try:
        disks = _xml(storage_reply).findall(".//hdd")
    except (ElementTree.ParseError, ValueError):
        found.append(Finding("warn", topic, "can't read the NVR's disks"))
    else:
        if not disks:
            found.append(Finding("fail", topic, "no disk: the NVR is not recording"))
        for disk in disks:
            status = (disk.findtext("status") or "?").strip()
            name = disk.findtext("id") or "?"
            capacity = disk.findtext("capacity")
            text = f"disk {name}: {status}" + (f", {int(capacity) // 1024} GB" if capacity else "")
            found.append(Finding("ok" if status.lower() in DISK_OK else "fail", topic, text))
    return found


def check_nvrs(
    cameras: Iterable[CameraConfig],
    credentials: dict[str, Credential],
    branch_tz: str,
    transport: httpx.BaseTransport | None = None,
) -> list[Finding]:
    seen: dict[tuple[str, int], NvrRef] = {}
    for camera in cameras:
        if camera.nvr is not None:
            seen.setdefault((camera.nvr.host, camera.nvr.http_port), camera.nvr)
    found = []
    for nvr in seen.values():
        credential = credentials.get(nvr.credentials_ref) if nvr.credentials_ref else None
        found += check_nvr(nvr, credential, branch_tz, transport)
    return found


# --- all --------------------------------------------------------------------------------


def run(
    settings: EdgeSettings,
    client: CloudClient,
    probe: Prober | None = None,
    ntp: Callable[[], bool | None] = ntp_synced,
    transport: httpx.BaseTransport | None = None,
) -> list[Finding]:
    found = check_box(settings, ntp)
    cloud, config = check_cloud(settings, client, transport)
    found += cloud
    if config is None:
        config = ConfigStore(settings.config_path).load()
        if config is not None:
            found.append(Finding("warn", "config", "using the last config saved on the box"))
    if config is None:
        found.append(Finding("fail", "config", "no config: cameras can't be checked"))
        return found
    credentials = load_credentials(settings.credentials_file)
    if not credentials and any(c.credentials_ref for c in config.cameras):
        found.append(Finding("fail", "credentials", f"{settings.credentials_file} is missing"))
    if not config.cameras:
        found.append(Finding("warn", "cameras", "no cameras in the config yet"))
    probe = probe or ffprobe(_ffprobe_binary(settings.ffmpeg))
    found += check_cameras(config.cameras, credentials, probe)
    found += check_nvrs(config.cameras, credentials, config.timezone, transport)
    return found


def _ffprobe_binary(ffmpeg_bin: str) -> str:
    path = Path(ffmpeg_bin)
    return str(path.with_name("ffprobe" + path.suffix)) if path.parent != Path() else "ffprobe"


def report(findings: list[Finding], as_json: bool = False) -> str:
    if as_json:
        return json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=2)
    width = max((len(f.topic) for f in findings), default=0)
    return "\n".join(f"{f.level.upper():4}  {f.topic:<{width}}  {f.message}" for f in findings)
