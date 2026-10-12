"""Regression coverage for bounded parallel download admission."""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing
import os
import signal
import time
import threading
from pathlib import Path

import pytest


def _set_download_limits(settings, execution: int) -> None:
    settings.download_execution_concurrency = execution
    settings.download_concurrency_cap = 5
    settings.download_device_concurrency_limit = 5


@pytest.fixture
def download_setup(monkeypatch, tmp_path):
    redis = pytest.importorskip("redis")
    from app.config import settings
    from app.services import heavy_io

    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.ping()
    except redis.RedisError:
        client.close()
        pytest.skip("Redis is unavailable")
    reservation_keys = tuple(getattr(heavy_io, "DOWNLOAD_RESERVATION_KEYS", ()))
    if len(reservation_keys) != 5:
        pytest.fail("download admission must expose exactly five reservation slots")
    for key in set(heavy_io.RESOURCE_RESERVATION_KEYS) | set(reservation_keys):
        client.delete(key)
    client.delete(heavy_io.HEAVY_IO_LOCK_KEY, "resource:lanes:last-background")
    for field in ("download_execution_concurrency", "download_concurrency_cap", "download_device_concurrency_limit"):
        monkeypatch.setattr(settings, field, getattr(settings, field))
    _set_download_limits(settings, 4)
    monkeypatch.setattr(heavy_io, "_local_lock_path", lambda: tmp_path / "heavy-io.lock")

    async def permitted(_workload):
        return True, {}, {}

    async def noop_state(*_args, **_kwargs):
        return None

    monkeypatch.setattr(heavy_io, "resource_profile_permitted", permitted)
    monkeypatch.setattr(heavy_io, "set_resource_state", noop_state)
    monkeypatch.setattr(
        heavy_io,
        "resource_reservation_details",
        lambda *_args: (1, 8 * 768 * 1024**2),
    )
    yield client
    for key in set(heavy_io.RESOURCE_RESERVATION_KEYS) | set(reservation_keys):
        client.delete(key)
    client.delete(heavy_io.HEAVY_IO_LOCK_KEY, "resource:lanes:last-background")
    client.close()


def _download_competitor(redis_url, lock_path, barrier, active, peak, queue, index, limit):
    import redis
    from app.config import settings
    from app.services import heavy_io

    _set_download_limits(settings, limit)
    heavy_io._local_lock_path = lambda: Path(lock_path)

    async def permitted(_workload):
        return True, {}, {}

    async def noop_state(*_args, **_kwargs):
        return None

    async def run():
        heavy_io.resource_profile_permitted = permitted
        heavy_io.resource_reservation_details = lambda *_args: (1, 8 * 768 * 1024**2)
        heavy_io.set_resource_state = noop_state
        client = redis.Redis.from_url(redis_url)
        barrier.wait()
        try:
            async with heavy_io.parallel_download_slot(
                "download_network", f"download:child:{index}",
                source_identity=f"archive:child:{index}", redis_client=client,
                lease_seconds=5, renew_seconds=30, poll_seconds=0.05,
            ):
                with active.get_lock():
                    active.value += 1
                    peak.value = max(peak.value, active.value)
                time.sleep(0.35)
                with active.get_lock():
                    active.value -= 1
                queue.put("entered")
        except heavy_io.HeavyIOUnavailable as exc:
            queue.put(f"denied:{exc.reason}")
        finally:
            client.close()

    asyncio.run(run())


def _orphan_download_slot(redis_url, lock_path, ready, queue):
    import redis
    from app.config import settings
    from app.services import heavy_io

    _set_download_limits(settings, 2)
    heavy_io._local_lock_path = lambda: Path(lock_path)

    async def permitted(_workload):
        return True, {}, {}

    async def noop_state(*_args, **_kwargs):
        return None

    async def run():
        heavy_io.resource_profile_permitted = permitted
        heavy_io.resource_reservation_details = lambda *_args: (1, 8 * 768 * 1024**2)
        heavy_io.set_resource_state = noop_state
        client = redis.Redis.from_url(redis_url)
        async with heavy_io.parallel_download_slot(
            "download_network", "download:orphan", source_identity="archive:orphan",
            redis_client=client, lease_seconds=1, renew_seconds=30, poll_seconds=0.05,
        ):
            ready.set()
            queue.put("entered")
            queue.close()
            queue.join_thread()
            time.sleep(30)

    asyncio.run(run())


@pytest.mark.asyncio
async def test_download_reservation_layout_and_network_statistics(download_setup):
    from app.services import heavy_io

    assert len(heavy_io.DOWNLOAD_RESERVATION_KEYS) == 5
    assert heavy_io.DOWNLOAD_RESERVATION_KEYS == tuple(
        f"lock:resource-budget:network:{slot}" for slot in range(5)
    )
    contexts = []
    try:
        for index in range(2):
            context = heavy_io.parallel_download_slot(
                "download_network", f"download:stats:{index}",
                source_identity=f"archive:stats:{index}", redis_client=download_setup,
            )
            await context.__aenter__()
            contexts.append(context)
        summary = heavy_io.collect_active_resource_leases(download_setup)
        assert summary["active_count"] == 2
        assert summary["network_active_count"] == 2
        assert summary["network_active"] is True
    finally:
        for context in reversed(contexts):
            await context.__aexit__(None, None, None)


@pytest.mark.parametrize("execution", [2, 4])
@pytest.mark.asyncio
async def test_download_slots_respect_execution_cap_and_release(download_setup, execution):
    from app.config import settings
    from app.services import heavy_io

    _set_download_limits(settings, execution)
    contexts = []
    try:
        for index in range(execution):
            context = heavy_io.parallel_download_slot(
                "download_network", f"download:cap:{execution}:{index}",
                source_identity=f"archive:cap:{execution}:{index}", redis_client=download_setup,
            )
            await context.__aenter__()
            contexts.append(context)
        assert heavy_io.collect_active_resource_leases(download_setup)["network_active_count"] == execution
        with pytest.raises(heavy_io.HeavyIOUnavailable):
            async with heavy_io.parallel_download_slot(
                "download_network", f"download:cap:{execution}:blocked",
                source_identity=f"archive:cap:{execution}:blocked", redis_client=download_setup,
            ):
                pytest.fail("download admission exceeded the configured execution cap")
        released = contexts.pop()
        await released.__aexit__(None, None, None)
        async with heavy_io.parallel_download_slot(
            "download_network", f"download:cap:{execution}:replacement",
            source_identity=f"archive:cap:{execution}:replacement", redis_client=download_setup,
        ):
            assert heavy_io.collect_active_resource_leases(download_setup)["network_active_count"] == execution
    finally:
        for context in reversed(contexts):
            await context.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_same_archive_is_exclusive_but_different_archives_overlap(download_setup):
    from app.services import heavy_io

    async with heavy_io.parallel_download_slot(
        "download_network", "download:archive:first",
        source_identity="gallerydl-archive:danbooru", redis_client=download_setup,
    ):
        with pytest.raises(heavy_io.HeavyIOUnavailable):
            async with heavy_io.parallel_download_slot(
                "download_network", "download:archive:same",
                source_identity="gallerydl-archive:danbooru", redis_client=download_setup,
            ):
                pytest.fail("two writers entered the same gallery-dl archive")
        async with heavy_io.parallel_download_slot(
            "download_network", "download:archive:other",
            source_identity="gallerydl-archive:pixiv", redis_client=download_setup,
        ):
            assert heavy_io.collect_active_resource_leases(download_setup)["network_active_count"] == 2


@pytest.mark.asyncio
async def test_resource_pressure_blocks_then_allows_download(download_setup, monkeypatch):
    from app.services import heavy_io

    capacity = {"bytes": 0}
    monkeypatch.setattr(heavy_io, "resource_reservation_details", lambda *_args: (1024, capacity["bytes"]))
    with pytest.raises(heavy_io.HeavyIOUnavailable):
        async with heavy_io.parallel_download_slot(
            "download_network", "download:pressure:blocked",
            source_identity="archive:pressure:blocked", redis_client=download_setup,
        ):
            pytest.fail("download entered while the memory budget was exhausted")
    capacity["bytes"] = heavy_io.PARALLEL_DOWNLOAD_RESERVATION_BYTES
    async with heavy_io.parallel_download_slot(
        "download_network", "download:pressure:released",
        source_identity="archive:pressure:released", redis_client=download_setup,
    ):
        assert heavy_io.collect_active_resource_leases(download_setup)["network_active_count"] == 1


@pytest.mark.asyncio
async def test_cancellation_releases_download_slot(download_setup):
    from app.services import heavy_io

    ready = asyncio.Event()

    async def holder():
        async with heavy_io.parallel_download_slot(
            "download_network", "download:cancelled", source_identity="archive:cancelled",
            redis_client=download_setup,
        ):
            ready.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(holder())
    await asyncio.wait_for(ready.wait(), timeout=2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not any(download_setup.exists(key) for key in heavy_io.DOWNLOAD_RESERVATION_KEYS)
    async with heavy_io.parallel_download_slot(
        "download_network", "download:cancelled:replacement",
        source_identity="archive:cancelled:replacement", redis_client=download_setup,
    ):
        pass


@pytest.mark.asyncio
async def test_download_slots_keep_posix_cap_after_redis_key_loss(download_setup, tmp_path):
    from app.config import settings
    from app.services import heavy_io

    _set_download_limits(settings, 2)
    held = heavy_io.parallel_download_slot(
        "download_network", "download:parent", source_identity="archive:parent",
        redis_client=download_setup,
    )
    await held.__aenter__()
    download_setup.delete(*heavy_io.DOWNLOAD_RESERVATION_KEYS)
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(3)
    active = ctx.Value("i", 1)
    peak = ctx.Value("i", 1)
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_download_competitor,
            args=(settings.redis_url, str(tmp_path / "heavy-io.lock"), barrier, active, peak, result_queue, index, 2),
        )
        for index in range(3)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        results = [result_queue.get(timeout=2) for _ in processes]
        assert results.count("entered") == 1
        assert peak.value <= 2
    finally:
        await held.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_orphaned_process_slot_recovers_after_lease_expiry(download_setup, tmp_path):
    from app.config import settings
    from app.services import heavy_io

    ctx = multiprocessing.get_context("fork")
    ready = ctx.Event()
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=_orphan_download_slot,
        args=(settings.redis_url, str(tmp_path / "heavy-io.lock"), ready, result_queue),
    )
    process.start()
    assert ready.wait(3)
    assert result_queue.get(timeout=2) == "entered"
    os.kill(process.pid, signal.SIGKILL)
    process.join(5)
    assert process.exitcode == -signal.SIGKILL
    # Advance only this dead test actor's Redis lease expiry; production keeps
    # the existing minimum TTL and its recovery policy.
    for key in (*heavy_io.DOWNLOAD_RESERVATION_KEYS, heavy_io.source_lock_key("archive:orphan")):
        if download_setup.exists(key):
            download_setup.expire(key, 1)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and any(download_setup.exists(key) for key in heavy_io.DOWNLOAD_RESERVATION_KEYS):
        await asyncio.sleep(0.1)
    assert not any(download_setup.exists(key) for key in heavy_io.DOWNLOAD_RESERVATION_KEYS)
    async with heavy_io.parallel_download_slot(
        "download_network", "download:orphan:replacement",
        source_identity="archive:orphan", redis_client=download_setup,
    ):
        pass


@pytest.mark.asyncio
async def test_cancellation_during_source_lease_setup_cleans_all_keys(download_setup):
    from app.services import heavy_io

    source_key = heavy_io.source_lock_key("archive:cancel-race")
    started = threading.Event()
    release = threading.Event()
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()

    class BlockingRedis:
        def __getattr__(self, name):
            return getattr(download_setup, name)

        def set(self, key, *args, **kwargs):
            if str(key) == source_key:
                started.set()
                release.wait(3)
            return download_setup.set(key, *args, **kwargs)

        def eval(self, script, count, *args):
            if script == heavy_io._RELEASE_SCRIPT and args[0] in heavy_io.DOWNLOAD_RESERVATION_KEYS:
                cleanup_started.set()
                cleanup_release.wait(3)
            return download_setup.eval(script, count, *args)

    async def acquire():
        async with heavy_io.parallel_download_slot(
            "download_network", "download:cancel-race",
            source_identity="archive:cancel-race",
            redis_client=BlockingRedis(),
            lease_seconds=5,
            renew_seconds=30,
            poll_seconds=0.05,
        ):
            pytest.fail("cancelled admission unexpectedly entered the download body")

    task = asyncio.create_task(acquire())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        assert await asyncio.to_thread(cleanup_started.wait, 2)
        task.cancel()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        cleanup_release.set()
        if not task.done():
            with contextlib.suppress(BaseException):
                await task
    assert not download_setup.exists(source_key)
    assert not any(download_setup.exists(key) for key in heavy_io.DOWNLOAD_RESERVATION_KEYS)


@pytest.mark.asyncio
async def test_reducing_download_limit_counts_live_retired_slots(download_setup):
    from app.config import settings
    from app.services import heavy_io

    contexts = []
    try:
        for index in range(3):
            context = heavy_io.parallel_download_slot(
                "download_network", f"download:retired:{index}",
                source_identity=f"archive:retired:{index}", redis_client=download_setup,
            )
            await context.__aenter__()
            contexts.append(context)
        settings.download_execution_concurrency = 2
        download_setup.delete(*heavy_io.DOWNLOAD_RESERVATION_KEYS)
        with pytest.raises(heavy_io.HeavyIOUnavailable):
            async with heavy_io.parallel_download_slot(
                "download_network", "download:too-many", source_identity="archive:new", redis_client=download_setup,
            ):
                pytest.fail("a reduced pool admitted work while three old slots were live")
        for context in contexts[:2]:
            await context.__aexit__(None, None, None)
        contexts = contexts[2:]
        async with heavy_io.parallel_download_slot(
            "download_network", "download:replacement", source_identity="archive:new", redis_client=download_setup,
        ):
            pass
    finally:
        for context in reversed(contexts):
            await context.__aexit__(None, None, None)
