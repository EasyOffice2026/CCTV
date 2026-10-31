"""The relay and pub/sub, with Redis (fakeredis) and in-process. No database needed."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from cctv_cloud.services.notify import LocalBus, RedisSubscriber
from cctv_cloud.services.relay import LocalRelay, RedisRelay
from cctv_contracts.v1 import FrameRequest, RemoteReply, RemoteRequest

fakeredis = pytest.importorskip("fakeredis")


def relays():
    yield "local", lambda: LocalRelay()
    yield (
        "redis",
        lambda: RedisRelay.from_client(
            fakeredis.FakeAsyncRedis(decode_responses=True, lua_modules=set())
        ),
    )


def request(seconds: float = 5) -> RemoteRequest:
    return RemoteRequest(
        id=uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        request=FrameRequest(camera_id=uuid.uuid4()),
    )


@pytest.fixture(params=[name for name, _ in relays()])
def make_relay(request):
    return dict(relays())[request.param]


def test_request_reaches_the_box_and_the_reply_comes_back(make_relay):
    async def scenario():
        relay = make_relay()
        box, other = uuid.uuid4(), uuid.uuid4()
        sent = request()
        await relay.submit(box, sent)
        assert await relay.next_request(other, 0.05) is None
        received = await relay.next_request(box, 1)
        assert received == sent

        reply = RemoteReply(ok=True, jpeg_base64="/9j/")
        waiter = asyncio.ensure_future(relay.wait_reply(sent.id, 2))
        assert not await relay.reply(other, sent.id, RemoteReply(ok=False, error="evil"))
        assert await relay.reply(box, sent.id, reply)
        assert await waiter == reply
        assert not await relay.reply(box, sent.id, reply)  # only once

    asyncio.run(scenario())


def test_expired_requests_are_skipped_and_waits_time_out(make_relay):
    async def scenario():
        relay = make_relay()
        box = uuid.uuid4()
        stale = request(seconds=0.05)
        await relay.submit(box, stale)
        await asyncio.sleep(0.1)
        assert await relay.next_request(box, 0.2) is None
        assert await relay.wait_reply(stale.id, 0.1) is None

    asyncio.run(scenario())


def test_local_bus_delivers_published_messages():
    async def scenario():
        bus = LocalBus()
        async with bus.subscribe("events:t") as messages:
            # publish() is called from worker threads by the API.
            await asyncio.to_thread(bus.publish, "events:t", {"event_id": uuid.uuid4()})
            await asyncio.to_thread(bus.publish, "events:other", {"event_id": "no"})
            message = await asyncio.wait_for(anext(messages), 1)
            assert isinstance(message["event_id"], str)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(messages), 0.1)

    asyncio.run(scenario())


def test_redis_subscriber_decodes_messages():
    async def scenario():
        client = fakeredis.FakeAsyncRedis()
        subscriber = RedisSubscriber.from_client(client)
        async with subscriber.subscribe("events:t") as messages:
            await client.publish("events:t", '{"event_id": "e1"}')
            assert await asyncio.wait_for(anext(messages), 1) == {"event_id": "e1"}

    asyncio.run(scenario())
