"""Passing requests from browsers to edge boxes and their replies back.

Edge boxes sit behind the restaurant's NAT, so the cloud can't call them. Each box
long-polls for requests (`GET /v1/edge/remote`); the browser's request waits for the
box's reply. With several API processes the two sides may be in different processes, so
the queues live in Redis:

    relay:q:<device_id>     list of pending RemoteRequest JSON (BLPOP by the box)
    relay:owner:<request>   the device a request was sent to (only it may reply)
    relay:r:<request>       the RemoteReply JSON (BLPOP by the waiting API request)

Without Redis an in-process version does the same for a single API process.
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Protocol

import redis.asyncio

from cctv_cloud.config import get_settings
from cctv_contracts.v1 import RemoteReply, RemoteRequest

REPLY_TTL_SECONDS = 30

# KEYS: owner key, reply key. ARGV: device id, reply JSON, reply TTL.
# Only the box the request was sent to may reply, and only once.
_REPLY_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('DEL', KEYS[1])
redis.call('RPUSH', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return 1
"""


class EdgeRelay(Protocol):
    async def submit(self, device_id: uuid.UUID, request: RemoteRequest) -> None: ...

    async def next_request(self, device_id: uuid.UUID, timeout: float) -> RemoteRequest | None: ...

    async def reply(
        self, device_id: uuid.UUID, request_id: uuid.UUID, reply: RemoteReply
    ) -> bool: ...

    async def wait_reply(self, request_id: uuid.UUID, timeout: float) -> RemoteReply | None: ...


def _seconds_left(request: RemoteRequest) -> float:
    return (request.expires_at - datetime.now(timezone.utc)).total_seconds()


class RedisRelay:
    def __init__(self, url: str) -> None:
        # No socket timeout: BLPOP blocks for up to the long-poll time.
        self._redis = redis.asyncio.Redis.from_url(
            url, socket_connect_timeout=2, decode_responses=True
        )

    @classmethod
    def from_client(cls, client: redis.asyncio.Redis) -> "RedisRelay":
        """For tests: a relay on an existing client (created with decode_responses=True)."""
        relay = cls.__new__(cls)
        relay._redis = client
        return relay

    async def submit(self, device_id: uuid.UUID, request: RemoteRequest) -> None:
        ttl = max(1, int(_seconds_left(request)) + 5)
        queue = f"relay:q:{device_id}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(f"relay:owner:{request.id}", str(device_id), ex=ttl)
            pipe.rpush(queue, request.model_dump_json())
            pipe.expire(queue, ttl)
            await pipe.execute()

    async def next_request(self, device_id: uuid.UUID, timeout: float) -> RemoteRequest | None:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            item = await self._redis.blpop([f"relay:q:{device_id}"], timeout=max(left, 0.01))
            if item is None:
                return None
            request = RemoteRequest.model_validate_json(item[1])
            if _seconds_left(request) > 0:
                return request
        return None

    async def reply(self, device_id: uuid.UUID, request_id: uuid.UUID, reply: RemoteReply) -> bool:
        # One atomic step, so another box's reply can't consume the owner record.
        accepted = await self._redis.eval(
            _REPLY_SCRIPT,
            2,
            f"relay:owner:{request_id}",
            f"relay:r:{request_id}",
            str(device_id),
            reply.model_dump_json(),
            REPLY_TTL_SECONDS,
        )
        return accepted == 1

    async def wait_reply(self, request_id: uuid.UUID, timeout: float) -> RemoteReply | None:
        item = await self._redis.blpop([f"relay:r:{request_id}"], timeout=timeout)
        return RemoteReply.model_validate_json(item[1]) if item else None


class LocalRelay:
    """Single-process relay; all methods must run on the same event loop."""

    def __init__(self) -> None:
        self._queues: dict[uuid.UUID, asyncio.Queue[RemoteRequest]] = {}
        self._owners: dict[uuid.UUID, uuid.UUID] = {}
        self._replies: dict[uuid.UUID, asyncio.Future[RemoteReply]] = {}

    async def submit(self, device_id: uuid.UUID, request: RemoteRequest) -> None:
        self._owners[request.id] = device_id
        self._replies[request.id] = asyncio.get_running_loop().create_future()
        self._queues.setdefault(device_id, asyncio.Queue()).put_nowait(request)

    async def next_request(self, device_id: uuid.UUID, timeout: float) -> RemoteRequest | None:
        queue = self._queues.setdefault(device_id, asyncio.Queue())
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                request = await asyncio.wait_for(queue.get(), left)
            except TimeoutError:
                return None
            if _seconds_left(request) > 0:
                return request
            self._forget(request.id)
        return None

    async def reply(self, device_id: uuid.UUID, request_id: uuid.UUID, reply: RemoteReply) -> bool:
        if self._owners.get(request_id) != device_id:
            return False
        del self._owners[request_id]
        future = self._replies.get(request_id)
        if future is not None and not future.done():
            future.set_result(reply)
        return True

    async def wait_reply(self, request_id: uuid.UUID, timeout: float) -> RemoteReply | None:
        future = self._replies.get(request_id)
        if future is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            return None
        finally:
            self._forget(request_id)

    def _forget(self, request_id: uuid.UUID) -> None:
        self._owners.pop(request_id, None)
        self._replies.pop(request_id, None)


@lru_cache
def get_relay() -> EdgeRelay:
    url = get_settings().redis_url
    return RedisRelay(url) if url else LocalRelay()
