"""Client for the local go2rtc HTTP API (live view and NVR playback over WebRTC).

go2rtc runs next to the edge service (infra/edge/docker-compose.yml) with its API on
localhost only. Streams are added at runtime with the camera credentials in the source
URL, so the credentials never go into a config file; its config is mounted read-only.
Error messages never include the request URL, whose query holds the source.
"""

import httpx

from cctv_edge.credentials import redact


class Go2rtcError(RuntimeError):
    pass


class Go2rtc:
    def __init__(
        self, base_url: str, timeout: float = 10, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, action: str, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise Go2rtcError(f"go2rtc {action}: {type(exc).__name__}") from None

    @staticmethod
    def _fail(action: str, response: httpx.Response) -> Go2rtcError:
        body = redact(response.text.strip())[:200]
        return Go2rtcError(f"go2rtc {action}: HTTP {response.status_code} {body}".rstrip())

    def ensure_stream(self, name: str, src: str) -> None:
        """Add or replace stream `name` reading from `src` (which may hold credentials)."""
        response = self._request(
            "add stream", "PUT", "/api/streams", params={"name": name, "src": src}
        )
        if response.status_code < 400:
            return
        # go2rtc creates the stream in memory, then tries to save it to its config file.
        # With the config mounted read-only that save fails (and should: it would write
        # the credentials to disk), so check whether the stream exists before giving up.
        if self.has_stream(name):
            return
        raise self._fail("add stream", response)

    def has_stream(self, name: str) -> bool:
        response = self._request("get stream", "GET", "/api/streams", params={"src": name})
        return response.status_code == 200

    def webrtc_answer(self, name: str, sdp_offer: str) -> str:
        """Send the browser's SDP offer for stream `name`; returns go2rtc's SDP answer."""
        response = self._request(
            "webrtc",
            "POST",
            "/api/webrtc",
            params={"src": name},
            content=sdp_offer.encode(),
            headers={"Content-Type": "application/sdp"},
        )
        if response.status_code >= 400:
            raise self._fail("webrtc", response)
        answer = response.text
        if not answer.startswith("v=0"):
            raise Go2rtcError("go2rtc webrtc: the answer is not SDP")
        return answer

    def delete_stream(self, name: str) -> None:
        response = self._request("delete stream", "DELETE", "/api/streams", params={"src": name})
        if response.status_code >= 400 and response.status_code != 404:
            raise self._fail("delete stream", response)
