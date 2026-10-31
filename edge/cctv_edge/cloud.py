"""HTTP client for the edge <-> cloud API (`cctv_contracts.v1`)."""

import json
import logging
from pathlib import Path
from typing import TypeVar
from uuid import UUID

import httpx
from pydantic import ValidationError

from cctv_contracts.v1 import (
    API_PREFIX,
    ContractModel,
    EdgeConfig,
    EventBatch,
    EventBatchResponse,
    Heartbeat,
    HeartbeatResponse,
    RemoteReply,
    RemoteRequest,
    UploadUrlRequest,
    UploadUrlResponse,
)
from cctv_edge import __version__

log = logging.getLogger(__name__)

T = TypeVar("T", bound=ContractModel)

# Statuses worth retrying; any other 4xx means the request itself is wrong.
_RETRYABLE = {401, 403, 408, 409, 425, 429}


class CloudError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status >= 500 or self.status in _RETRYABLE


class CloudClient:
    def __init__(
        self,
        base_url: str,
        device_token: str,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._api = httpx.Client(
            base_url=base_url.rstrip("/") + API_PREFIX,
            headers={
                "Authorization": f"Bearer {device_token}",
                "User-Agent": f"cctv-edge/{__version__}",
            },
            timeout=timeout,
            transport=transport,
        )
        # Pre-signed storage URLs carry their own auth; never send the device token there.
        self._storage = httpx.Client(timeout=max(timeout, 120), transport=transport)

    def close(self) -> None:
        self._api.close()
        self._storage.close()

    def _call(self, client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            # Drop the query string: on pre-signed URLs it holds the signature.
            target = httpx.URL(url).copy_with(query=None)
            raise CloudError(
                f"{method} {target} -> {response.status_code}: {response.text[:200]}",
                response.status_code,
            )
        return response

    def _post(self, path: str, body: ContractModel, out: type[T]) -> T:
        response = self._call(self._api, "POST", path, json=body.model_dump(mode="json"))
        return _parse(out, response)

    def fetch_config(self, current_version: int | None) -> EdgeConfig | None:
        """The resolved config, or None when `current_version` is still the latest."""
        headers = {"If-None-Match": f'"{current_version}"'} if current_version else {}
        response = self._call(self._api, "GET", "/config", headers=headers)
        if response.status_code == 304:
            return None
        return _parse(EdgeConfig, response)

    def send_events(self, batch: EventBatch) -> EventBatchResponse:
        return self._post("/events", batch, EventBatchResponse)

    def upload_url(self, request: UploadUrlRequest) -> UploadUrlResponse:
        return self._post("/media/upload-urls", request, UploadUrlResponse)

    def upload(self, target: UploadUrlResponse, path: Path, content_type: str) -> None:
        # Read whole files (clips are <= 100 MB): S3-style PUTs need a Content-Length.
        headers = {"Content-Type": content_type, **target.headers}
        content = path.read_bytes()
        self._call(
            self._storage, target.method, target.upload_url, content=content, headers=headers
        )

    def heartbeat(self, heartbeat: Heartbeat) -> HeartbeatResponse:
        return self._post("/heartbeat", heartbeat, HeartbeatResponse)

    def next_remote_request(self, wait_seconds: int) -> RemoteRequest | None:
        """Long poll for a request from the cloud (live view, playback, ...); None when
        nothing arrived within `wait_seconds`."""
        response = self._call(
            self._api,
            "GET",
            "/remote",
            params={"wait": wait_seconds},
            timeout=httpx.Timeout(self._timeout, read=wait_seconds + 15),
        )
        if response.status_code == 204:
            return None
        try:
            return RemoteRequest.model_validate_json(response.content)
        except ValidationError as exc:
            # A kind this box doesn't know yet (newer cloud): say so instead of going silent.
            try:
                request_id = UUID(str(json.loads(response.content)["id"]))
            except (ValueError, KeyError, TypeError) as parse_exc:
                raise CloudError(f"unexpected remote request: {exc}", 200) from parse_exc
            log.warning("remote request %s not understood: %s", request_id, exc)
            self.reply_remote(request_id, RemoteReply(ok=False, error="unsupported request"))
            return None

    def reply_remote(self, request_id: UUID, reply: RemoteReply) -> None:
        self._call(
            self._api,
            "POST",
            f"/remote/{request_id}/reply",
            content=reply.model_dump_json(exclude_none=True),
            headers={"Content-Type": "application/json"},
        )


def _parse(model: type[T], response: httpx.Response) -> T:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        raise CloudError(f"unexpected response from cloud: {exc}", response.status_code) from exc
