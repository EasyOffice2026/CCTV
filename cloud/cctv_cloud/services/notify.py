"""Realtime notifications over Redis pub/sub.

Channels:
    events:<tenant_id>   an event was created or updated (dashboard live feed, alerts)
    config:<branch_id>   the branch's edge config changed (the edge re-pulls)

Publishing is best effort: the data is already committed, and the edge also learns about
new config from the heartbeat, so a Redis outage only delays things.
"""

import json
import logging
from functools import lru_cache
from typing import Any, Protocol

import redis

from cctv_cloud.config import get_settings

log = logging.getLogger(__name__)


class Publisher(Protocol):
    def publish(self, channel: str, message: dict[str, Any]) -> None: ...


class NullPublisher:
    def publish(self, channel: str, message: dict[str, Any]) -> None:
        pass


class RedisPublisher:
    def __init__(self, url: str) -> None:
        self._client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)

    def publish(self, channel: str, message: dict[str, Any]) -> None:
        try:
            self._client.publish(channel, json.dumps(message, default=str))
        except redis.RedisError as exc:
            log.warning("publish to %s failed: %s", channel, exc)


@lru_cache
def get_publisher() -> Publisher:
    url = get_settings().redis_url
    return RedisPublisher(url) if url else NullPublisher()
