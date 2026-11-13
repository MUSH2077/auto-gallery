"""Redis and POSIX coverage for bounded image MEDIA slots."""
import asyncio
import multiprocessing
import time
from pathlib import Path

import pytest


@pytest.fixture
def parallel_setup(monkeypatch, tmp_path):
    redis = pytest.importorskip("redis")
    from app.config import settings
    from app.services import heavy_io

    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.ping()
    except redis.RedisError:
        client.close()
        pytest.skip("Redis is unavailable")
    for key in heavy_io.RESOURCE_RESERVATION_KEYS:
        client.delete(key)
    client.delete(heavy_io.HEAVY_IO_LOCK_KEY, "resource:lanes:last-background")
    monkeypatch.setattr(settings, "media_worker_concurrency", 2)
    monkeypatch.setattr(heavy_io, "_local_lock_path", lambda: tmp_path / "heavy-io.lock")

    async def permitted(_workload):
        return True, {}, {}

    monkeypatch.setattr(heavy_io, "resource_profile_permitted", permitted)
    monkeypatch.setattr(
        heavy_io,
        "resource_reservation_details",
        lambda *_args: (1, 2 * 768 * 1024**2),
    )
    monkeypatch.setattr(heavy_io, "set_resource_state", _noop_state)
    yield client
    for key in heavy_io.RESOURCE_RESERVATION_KEYS:
        client.delete(key)
    client.delete(heavy_io.HEAVY_IO_LOCK_KEY, "resource:lanes:last-background")
    client.close()


async def _noop_state(*_args, **_kwargs):
    return None


def _media_competitor(redis_url, lock_path, barrier, active, peak, result_queue, index):
    import redis
    from app.config import settings
    from app.services import heavy_io

    settings.media_worker_concurrency = 4
    heavy_io._local_lock_path = lambda: Path(lock_path)

    async def permitted(_workload):
        return True, {}, {}

    async def state(*_args, **_kwargs):
        return None

    async def run():
        heavy_io.resource_profile_permitted = permitted
        heavy_io.resource_reservation_details = lambda *_args: (1, 8 * 768 * 1024**2)
        heavy_io.set_resource_state = state
        client = redis.Redis.from_url(redis_url)
        barrier.wait()
        try:
            async with heavy_io.parallel_media_slot(
                "image_derive", f"media-derivative:entrant-{index}", redis_client=client
            ):
                with active.get_lock():
                    active.value += 1
                    peak.value = max(peak.value, active.value)
                time.sleep(0.5)
                with active.get_lock():
                    active.value -= 1
                result_queue.put("entered")
        except heavy_io.HeavyIOUnavailable:
            result_queue.put("denied")
        finally:
            client.close()

    asyncio.run(run())



@pytest.mark.asyncio
async def test_media_slots_are_bounded_and_released(parallel_setup):
    from app.services import heavy_io

    async with heavy_io.parallel_media_slot(
        "image_derive", "media-derivative:a", redis_client=parallel_setup
    ):
        async with heavy_io.parallel_media_slot(
            "image_derive", "media-derivative:b", redis_client=parallel_setup
        ):
            summary = heavy_io.collect_active_resource_leases(parallel_setup)
            assert summary["active_count"] == 2
            assert summary["reserved_bytes"] == 2 * 768 * 1024**2
            assert summary["disk_active"]
            assert summary["background_active"]
            with pytest.raises(heavy_io.HeavyIOUnavailable):
                async with heavy_io.parallel_media_slot(
                    "image_derive", "media-derivative:c", redis_client=parallel_setup
                ):
                    pytest.fail("third image entered a two-slot pool")
        async with heavy_io.parallel_media_slot(
            "image_derive", "media-derivative:c", redis_client=parallel_setup
        ):
            pass
    assert not any(parallel_setup.exists(key) for key in heavy_io.MEDIA_RESERVATION_KEYS)


@pytest.mark.asyncio
async def test_media_slots_keep_legacy_exclusive_work_out(parallel_setup):
    from app.services import heavy_io

    async with heavy_io.parallel_media_slot(
        "image_derive", "media-derivative:a", redis_client=parallel_setup
    ):
        for workload in ("maintenance", "video_derive", "search_index"):
            locks = heavy_io.local_lock_for_workload(workload)
            try:
                assert not locks.try_acquire(), workload
            finally:
                locks.release()


@pytest.mark.asyncio
async def test_deleting_redis_slots_does_not_bypass_posix_cap(monkeypatch, parallel_setup):
    from app.config import settings
    from app.services import heavy_io

    monkeypatch.setattr(settings, "media_worker_concurrency", 1)
    async with heavy_io.parallel_media_slot(
        "image_derive", "media-derivative:a", redis_client=parallel_setup
    ):
        parallel_setup.delete(*heavy_io.MEDIA_RESERVATION_KEYS)
        with pytest.raises(heavy_io.HeavyIOUnavailable):
            async with heavy_io.parallel_media_slot(
                "image_derive", "media-derivative:b", redis_client=parallel_setup
            ):
                pytest.fail("lost Redis keys bypassed the live POSIX slot")


@pytest.mark.asyncio
async def test_parallel_path_preserves_resource_state_identities(monkeypatch, parallel_setup):
    from app.services import heavy_io

    states = []

    async def record(*args, **kwargs):
        states.append((args, kwargs))

    monkeypatch.setattr(heavy_io, "set_resource_state", record)
    async with heavy_io.heavy_io_slot(
        "image_derive",
        "media-derivative:identity",
        redis_client=parallel_setup,
        publisher_attempt="attempt-7",
        execution_token="execution-9",
    ):
        pass
    assert states[-1][0][1] == "running"
    assert states[-1][1]["publisher_attempt"] == "attempt-7"
    assert states[-1][1]["execution_token"] == "execution-9"


@pytest.mark.asyncio
async def test_eight_processes_cannot_exceed_four_after_redis_key_loss(monkeypatch, parallel_setup, tmp_path):
    from app.config import settings
    from app.services import heavy_io

    monkeypatch.setattr(settings, "media_worker_concurrency", 4)
    monkeypatch.setattr(
        heavy_io,
        "resource_reservation_details",
        lambda *_args: (1, 8 * 768 * 1024**2),
    )
    held = []
    for index in range(3):
        context = heavy_io.parallel_media_slot(
            "image_derive", f"media-derivative:held-{index}", redis_client=parallel_setup
        )
        held.append(context)
        await context.__aenter__()
    parallel_setup.delete(*heavy_io.MEDIA_RESERVATION_KEYS)

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(8)
    active = ctx.Value("i", 3)
    peak = ctx.Value("i", 3)
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_media_competitor,
            args=(
                settings.redis_url,
                str(tmp_path / "heavy-io.lock"),
                barrier,
                active,
                peak,
                result_queue,
                index,
            ),
        )
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=2) for _ in processes]
    assert "entered" in results
    assert peak.value <= 4
    for context in reversed(held):
        await context.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_remote_search_fence_blocks_media_slot(parallel_setup):
    from app.services import heavy_io
    from app.services.remote_search_flight import clear_marker, create_marker

    create_marker("media-fence-test", "search_index")
    try:
        with pytest.raises(heavy_io.HeavyIOUnavailable, match="remote_search_in_flight"):
            async with heavy_io.parallel_media_slot(
                "image_derive", "media-derivative:fenced", redis_client=parallel_setup
            ):
                pytest.fail("media slot crossed the durable remote fence")
    finally:
        clear_marker("media-fence-test")
