"""Download lifecycle Redis bridge semantics against the pinned RQ API."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from redis import Redis
from rq import Queue

from app.services.download_runtime import Bridge, DRAIN_KEY, CONTROLLER_KEY, QUEUES, queue_snapshot
from app.services.backpressure import DOWNLOAD_QUEUE_NAMES


@pytest.fixture
def connection():
    value = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    value.flushdb()
    yield value
    value.flushdb()
    value.close()


def test_snapshot_covers_actual_provider_queues_and_delayed_retry(connection):
    assert set(QUEUES) == set(DOWNLOAD_QUEUE_NAMES)
    queue = Queue("downloads:x", connection=connection)
    job = queue.enqueue(sum, [1, 2])
    future = queue.enqueue_at(datetime.now(timezone.utc) + timedelta(seconds=60), sum, [3])
    value = queue_snapshot(connection)
    assert value["runnable"] == 1
    assert value["inflight"] == 0
    assert value["queues"]["downloads:x"]["queued"] == 1
    assert 55 < value["next_due_in_s"] < 65
    assert len(value["queues"]) == 9
    assert job.id != future.id


def test_controller_lease_reconnect_and_exclusive_ownership(connection):
    first = Bridge(connection, "owner")
    assert first.command({"action":"acquire"})["owned"]
    reconnect = Bridge(connection, "owner")
    assert reconnect.command({"action":"acquire"})["owned"]
    other = Bridge(connection, "other")
    assert not other.command({"action":"acquire"})["owned"]
    first.command({"action":"drain", "drain_token":"owner:cycle1"})
    assert connection.get(DRAIN_KEY) == "owner:cycle1"
    assert 0 < connection.ttl(DRAIN_KEY) <= 15
    first.command({"action":"cancel"})
    assert connection.get(DRAIN_KEY) is None
    first.command({"action":"release"})
    assert connection.get(CONTROLLER_KEY) is None


def test_old_bridge_cannot_delete_a_new_drain_or_renew_lost_owner(connection):
    first = Bridge(connection, "old")
    first.command({"action":"acquire"})
    first.command({"action":"drain", "drain_token":"old:cycle"})
    connection.set(DRAIN_KEY, "new:cycle")
    connection.set(CONTROLLER_KEY, "new")
    with pytest.raises(RuntimeError):
        first.command({"action":"cancel"})
    assert connection.get(DRAIN_KEY) == "new:cycle"


def test_new_owner_clears_expired_controller_drain(connection):
    connection.set(DRAIN_KEY, "dead-controller:cycle", ex=15)
    new = Bridge(connection, "new")
    assert new.command({"action":"acquire"})["owned"]
    new.command({"action":"cancel"})
    assert connection.get(DRAIN_KEY) is None
