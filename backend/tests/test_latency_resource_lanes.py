"""Exercise admission and wake-up behavior against the isolated test Redis."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import redis

from app.config import settings
from app.services import heavy_io


@pytest.fixture
def lane_redis():
    client = redis.Redis.from_url(settings.redis_url)
    prefixes = ("lock:resource-", "resource:lanes:")

    def clean():
        client.delete("rq:queue:imports", "rq:wip:imports")
        for prefix in prefixes:
            keys = list(client.scan_iter(f"{prefix}*"))
            if keys:
                client.delete(*keys)

    clean()
    yield client
    clean()
    client.close()


def lease(client, workload, owner, size, capacity=500):
    return heavy_io.RenewableRedisLease(
        heavy_io.resource_lease_keys(workload),
        workload=workload,
        owner=owner,
        redis_client=client,
        reservation_key=heavy_io.resource_budget_token_key(workload),
        reservation_bytes=size,
        reservation_capacity_bytes=capacity,
    )


@pytest.mark.asyncio
async def test_indexing_does_not_exclude_an_import_with_available_memory(lane_redis):
    indexing = lease(lane_redis, "search_index", "search", 200)
    importing = lease(lane_redis, "import_db", "import", 100)
    try:
        assert await indexing.try_acquire()
        assert await importing.try_acquire(), "background indexing must not own the import lane"
    finally:
        await importing.release()
        await indexing.release()


@pytest.mark.asyncio
async def test_network_import_and_background_reservations_share_one_memory_floor(lane_redis):
    indexing = lease(lane_redis, "search_index", "search", 200, capacity=300)
    importing = lease(lane_redis, "import_db", "import", 100, capacity=300)
    network = lease(lane_redis, "download_network", "download", 120, capacity=300)
    try:
        assert await indexing.try_acquire()
        assert await importing.try_acquire()
        assert not await network.try_acquire()
        assert network.denial_reason == "resource_reservation_capacity"
        await indexing.release()
        assert await network.try_acquire()
    finally:
        await network.release()
        await importing.release()
        await indexing.release()


@pytest.mark.asyncio
async def test_media_and_indexing_still_share_one_background_slot(lane_redis):
    indexing = lease(lane_redis, "search_index", "search", 200)
    media = lease(lane_redis, "image_derive", "media", 100)
    try:
        assert await indexing.try_acquire()
        assert not await media.try_acquire()
    finally:
        await media.release()
        await indexing.release()


@pytest.mark.asyncio
async def test_ready_ingest_gets_priority_but_background_receives_a_turn_after_sixty_seconds(lane_redis):
    lane_redis.rpush("rq:queue:imports", "ready-import")
    background = lease(lane_redis, "search_index", "search", 200)
    try:
        assert not await background.try_acquire(), "new background work should yield to ready ingestion"
        assert background.denial_reason == "background_yield_to_ingest"
        # Simulate a turn that has waited, without a wall-clock minute in the suite.
        lane_redis.set("resource:lanes:last-background", str(lane_redis.time()[0] - 61))
        assert await background.try_acquire(), "continuous imports must not starve derived work"
        await background.release()
        assert not await background.try_acquire(), "one aged grant must not open an unlimited burst"
    finally:
        await background.release()


def test_local_disk_lanes_keep_maintenance_exclusive_when_redis_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HEAVY_IO_LOCK_PATH", str(tmp_path / "maintenance.lock"))
    importing = heavy_io.local_lock_for_workload("import_db")
    indexing = heavy_io.local_lock_for_workload("search_index")
    media = heavy_io.local_lock_for_workload("image_derive")
    maintenance = heavy_io.local_lock_for_workload("backup")
    try:
        assert importing.try_acquire()
        assert indexing.try_acquire()
        assert not media.try_acquire()
        assert not maintenance.try_acquire()
        importing.release()
        assert not maintenance.try_acquire()
        indexing.release()
        assert maintenance.try_acquire()
        assert not importing.try_acquire()
    finally:
        for lock in (maintenance, media, indexing, importing):
            lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["release", "control"])
async def test_releasing_capacity_wakes_import_waiter_without_poll_timeout(lane_redis, monkeypatch, event):
    from app.services.resource_pressure import RESOURCE_CONTROL_CHANNEL
    from app.services.redis_pubsub import TaskChannel

    monkeypatch.setattr(heavy_io, "_get_lease_redis", lambda: lane_redis)
    indexing = lease(lane_redis, "search_index", "search", 200)
    assert await indexing.try_acquire()
    waiter = asyncio.create_task(heavy_io._wait_for_resource_event("import_db", 10, task_id="waiting-import"))
    try:
        # Wait until the real subscriber has registered, independent of CPU speed.
        for _ in range(100):
            if lane_redis.pubsub_numsub(TaskChannel.control("waiting-import"))[0][1]:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("resource waiter did not subscribe")
        if event == "release":
            await indexing.release()
        else:
            lane_redis.publish(TaskChannel.control("waiting-import"), '{"command":"pause"}')
        await asyncio.wait_for(waiter, timeout=0.75)
    finally:
        # Unblock the SDK receiver even on the intentionally failing baseline.
        lane_redis.publish(RESOURCE_CONTROL_CHANNEL, "test-cleanup")
        if not waiter.done():
            waiter.cancel()
        await indexing.release()


@pytest.mark.asyncio
async def test_missed_resource_event_uses_the_bounded_fallback(lane_redis, monkeypatch):
    monkeypatch.setattr(heavy_io, "_get_lease_redis", lambda: lane_redis)
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(heavy_io._wait_for_resource_event("import_db", 0.1), timeout=0.5)
    assert asyncio.get_running_loop().time() - started >= 0.08, "subscription acknowledgments are not resource events"


@pytest.mark.asyncio
async def test_import_does_not_add_duty_cycle_sleep_after_two_seconds_of_work(monkeypatch):
    from app.jobs import import_runner
    from app.services import resource_pressure

    clock = [0.0]
    slept = []
    snapshot = {
        "status": "warning",
        "controller_mode": "constrained",
        "budget": {"governance_mode": "enforce", "effective_throughput_scale": 0.1},
    }

    async def limits(_workload):
        return SimpleNamespace(allowed=True, work_units=2, slice_seconds=2), snapshot

    @asynccontextmanager
    async def capacity(*_args, **_kwargs):
        yield

    async def state(*_args, **_kwargs):
        return None

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(import_runner, "current_profile_slice_limits", limits)
    monkeypatch.setattr(import_runner, "heavy_io_slot", capacity)
    monkeypatch.setattr(import_runner, "set_resource_state", state)
    monkeypatch.setattr(import_runner, "monotonic", lambda: clock[0])
    monkeypatch.setattr(resource_pressure.asyncio, "sleep", sleep)
    async with import_runner._import_resource_slice("import_db", "test-owner"):
        clock[0] = 2.0
    assert slept == [], f"import added unnecessary idle time: {slept}"
