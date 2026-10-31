"""Realtime notifications over Redis pub/sub.

Channels:
    events:<tenant_id>   an event was created or updated (dashboard live feed, alerts)
    config:<branch_id>   the branch's edge config changed (the edge re-pulls)

Publishing is best effort: the data is already committed, and the edge also learns about
new config from the heartbeat, so a Redis outage only delays things.

Without Redis (`CCTV_REDIS_URL` empty) an in-process bus is used, which only works with a
single API process: fine for development and tests.
"""

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import lru_cache
from typing import Any, Protocol

import redis
import redis.asyncio

from cctv_cloud.config import get_settings

log = logging.getLogger(__name__)

# A slow WebSocket client loses messages beyond this instead of growing memory.
SUBSCRIBER_QUEUE = 1000


class Publisher(Protocol):
    def publish(self, channel: str, message: dict[str, Any]) -> None: ...


class Subscriber(Protocol):
    def subscribe(
        self, channel: str
    ) -> AbstractAsyncContextManager[AsyncIterator[dict[str, Any]]]: ...


class RedisPublisher:
    def __init__(self, url: str) -> None:
        self._client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)

    def publish(self, channel: str, message: dict[str, Any]) -> None:
        try:
            self._client.publish(channel, json.dumps(message, default=str))
        except redis.RedisError as exc:
            log.warning("publish to %s failed: %s", channel, exc)


class RedisSubscriber:
    def __init__(self, url: str) -> None:
        self._client = redis.asyncio.Redis.from_url(url, socket_connect_timeout=2)

    @classmethod
    def from_client(cls, client: redis.asyncio.Redis) -> "RedisSubscriber":
        """For tests: a subscriber on an existing client."""
        subscriber = cls.__new__(cls)
        subscriber._client = client
        return subscriber

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel)
        try:
            yield _redis_messages(pubsub)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()


async def _redis_messages(pubsub: Any) -> AsyncIterator[dict[str, Any]]:
    async for item in pubsub.listen():
        if item.get("type") == "message":
            yield json.loads(item["data"])


class LocalBus:
    """In-process pub/sub. `publish` may be called from any thread (sync endpoints and
    background tasks run in a thread pool); subscribers live on the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}

    def publish(self, channel: str, message: dict[str, Any]) -> None:
        message = json.loads(json.dumps(message, default=str))  # same shape as via Redis
        with self._lock:
            targets = list(self._subscribers.get(channel, ()))
        for loop, queue in targets:
            with contextlib.suppress(RuntimeError):  # the subscriber's loop has closed
                loop.call_soon_threadsafe(_offer, queue, message)

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        entry = (asyncio.get_running_loop(), asyncio.Queue(SUBSCRIBER_QUEUE))
        with self._lock:
            self._subscribers.setdefault(channel, []).append(entry)
        try:
            yield _queue_messages(entry[1])
        finally:
            with self._lock:
                self._subscribers[channel].remove(entry)


def _offer(queue: asyncio.Queue, message: dict[str, Any]) -> None:
    if not queue.full():
        queue.put_nowait(message)


async def _queue_messages(queue: asyncio.Queue) -> AsyncIterator[dict[str, Any]]:
    while True:
        yield await queue.get()


@lru_cache
def _local_bus() -> LocalBus:
    return LocalBus()


@lru_cache
def get_publisher() -> Publisher:
    url = get_settings().redis_url
    return RedisPublisher(url) if url else _local_bus()


@lru_cache
def get_subscriber() -> Subscriber:
    url = get_settings().redis_url
    return RedisSubscriber(url) if url else _local_bus()
