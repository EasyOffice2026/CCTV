"""Hikvision NVR access: recorded-footage RTSP URLs and the list of recordings (ISAPI).

Hikvision quirk: the times in playback URLs and ISAPI searches end in "Z" but the NVR
reads and writes them as its *local* time. So times are converted to the branch time
zone before sending and parsed in it when received. This is only right when the NVR's
clock and time zone are set correctly (NTP, Asia/Kuwait; see plan §0).
"""

import re
import uuid
from datetime import datetime
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from cctv_contracts.v1 import NvrRef, RecordingSpan
from cctv_edge.credentials import Credential

# The search returns pages of up to this many; we stop after MAX_SPANS.
PAGE_SIZE = 100
MAX_SPANS = 1000
MAX_RESPONSE_BYTES = 1024 * 1024

_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
_NAMESPACE = re.compile(r"^\{[^}]*\}")


class NvrError(RuntimeError):
    pass


def track_id(nvr: NvrRef) -> str:
    """Recording track of the channel's main stream: channel 3 -> "301"."""
    return f"{nvr.channel}01"


def _local(ts: datetime, tz: str) -> datetime:
    return ts.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def playback_url(nvr: NvrRef, start: datetime, end: datetime, tz: str) -> str:
    """RTSP URL of the recording from `start` to `end` (UTC-aware), without credentials."""
    start_s = _local(start, tz).strftime(_TIME_FORMAT)
    end_s = _local(end, tz).strftime(_TIME_FORMAT)
    return (
        f"rtsp://{nvr.host}:{nvr.rtsp_port}/Streaming/tracks/{track_id(nvr)}"
        f"?starttime={start_s}&endtime={end_s}"
    )


def _search_body(nvr: NvrRef, start: datetime, end: datetime, tz: str, position: int) -> str:
    start_s = _local(start, tz).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = _local(end, tz).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CMSearchDescription>"
        f"<searchID>{uuid.uuid4()}</searchID>"
        f"<trackList><trackID>{track_id(nvr)}</trackID></trackList>"
        "<timeSpanList><timeSpan>"
        f"<startTime>{start_s}</startTime><endTime>{end_s}</endTime>"
        "</timeSpan></timeSpanList>"
        f"<maxResults>{PAGE_SIZE}</maxResults>"
        # Hikvision's spelling.
        f"<searchResultPostion>{position}</searchResultPostion>"
        "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>"
        "</metadataList>"
        "</CMSearchDescription>"
    )


def _strip_namespaces(root: ElementTree.Element) -> None:
    for element in root.iter():
        element.tag = _NAMESPACE.sub("", element.tag)


def _parse_time(value: str, tz: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):  # local time despite the Z (see module docstring)
        return datetime.fromisoformat(value[:-1]).replace(tzinfo=ZoneInfo(tz))
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(tz))


def search_recordings(
    nvr: NvrRef,
    credential: Credential | None,
    start: datetime,
    end: datetime,
    tz: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 15,
) -> list[RecordingSpan]:
    """Recorded spans of the camera's channel between `start` and `end`, in UTC,
    clipped to the range."""
    auth = httpx.DigestAuth(credential.username, credential.password) if credential else None
    url = f"http://{nvr.host}:{nvr.http_port}/ISAPI/ContentMgmt/search"
    spans: list[RecordingSpan] = []
    position = 0
    with httpx.Client(auth=auth, timeout=timeout, transport=transport) as http:
        while len(spans) < MAX_SPANS:
            try:
                response = http.post(
                    url,
                    content=_search_body(nvr, start, end, tz, position),
                    headers={"Content-Type": "application/xml"},
                )
            except httpx.HTTPError as exc:
                raise NvrError(f"NVR unreachable: {type(exc).__name__}") from None
            if response.status_code == 401:
                raise NvrError("NVR refused the credentials")
            if response.status_code >= 400:
                raise NvrError(f"NVR search failed: HTTP {response.status_code}")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise NvrError("NVR search response too large")
            try:
                # From the NVR on the branch LAN, size-capped above; expat refuses
                # entity-expansion bombs.
                root = ElementTree.fromstring(response.content)  # noqa: S314
            except ElementTree.ParseError as exc:
                raise NvrError("NVR search response is not valid XML") from exc
            _strip_namespaces(root)
            items = root.findall("./matchList/searchMatchItem")
            for item in items:
                begin = item.findtext("./timeSpan/startTime")
                finish = item.findtext("./timeSpan/endTime")
                if not begin or not finish:
                    continue
                try:
                    span_start = max(_parse_time(begin, tz), start)
                    span_end = min(_parse_time(finish, tz), end)
                except ValueError:
                    continue
                if span_end > span_start:
                    spans.append(RecordingSpan(start=span_start, end=span_end))
            status = (root.findtext("./responseStatusStrg") or "").strip().upper()
            if status != "MORE" or not items:
                break
            position += len(items)
    spans.sort(key=lambda s: s.start)
    return spans[:MAX_SPANS]
