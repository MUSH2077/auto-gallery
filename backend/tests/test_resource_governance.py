import asyncio
import inspect
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException


class _FakeLeaseRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, _keys, key, token, *args):
        if self.values.get(key) != token:
            return 0
        if "expire" in script:
            return 1
        if "del" in script:
            del self.values[key]
            return 1
        return 0


class _ReservationRedis(_FakeLeaseRedis):
    """Small semantic fake for the aggregate reservation Lua contract."""

    def eval(self, script, key_count, *values):
        from app.services import heavy_io

        keys = list(values[:key_count])
        args = list(values[key_count:])
        if script == heavy_io._RESERVE_SCRIPT:
            target = keys[0]
            token, requested, capacity, _ttl = args
            if target in self.values:
                return 0
            reserved = 0
            for key in keys[1:]:
                raw = self.values.get(key)
                if raw is not None:
                    reserved += int(json.loads(raw)["reserved_bytes"])
            if reserved + int(requested) > int(capacity):
                return -1
            self.values[target] = token
            return 1
        key, token = keys[0], args[0]
        if self.values.get(key) != token:
            return 0
        if script == heavy_io._RENEW_SCRIPT:
            return 1
        if script == heavy_io._RELEASE_SCRIPT:
            del self.values[key]
            return 1
        return 0


def test_aggregate_reservation_counts_network_and_disk_before_grant(monkeypatch):
    from app.services import heavy_io
    from app.services.resource_pressure import ResourcePressureStateMachine, ResourceSample

    mib = 1024 ** 2
    gib = 1024 ** 3
    monkeypatch.setattr(
        "app.services.resource_pressure.current_memory_available_bytes",
        lambda: 8 * gib,
    )
    snapshot = ResourcePressureStateMachine().update(
        ResourceSample(
            memory_total_bytes=8 * gib,
            memory_available_bytes=int(1.45 * gib),
            swap_total_bytes=6 * gib,
            swap_free_bytes=3 * gib,
        ),
        now=0,
    )
    redis = _ReservationRedis()

    async def exercise():
        requested, capacity = heavy_io.resource_reservation_details(
            snapshot,
            "download_network",
        )
        network = heavy_io.RenewableRedisLease(
            heavy_io.resource_lease_keys("download_network"),
            workload="download_network",
            owner="network",
            redis_client=redis,
            reservation_key=heavy_io.RESOURCE_NETWORK_TOKEN_KEY,
            reservation_bytes=requested,
            reservation_capacity_bytes=capacity,
        )
        assert requested == 128 * mib
        assert await network.try_acquire() is True

        requested, capacity = heavy_io.resource_reservation_details(snapshot, "import_db")
        disk = heavy_io.RenewableRedisLease(
            heavy_io.resource_lease_keys("import_db"),
            workload="import_db",
            owner="disk",
            redis_client=redis,
            reservation_key=heavy_io.RESOURCE_DISK_TOKEN_KEY,
            reservation_bytes=requested,
            reservation_capacity_bytes=capacity,
        )
        assert requested == 96 * mib
        assert await disk.try_acquire() is False
        assert disk.denial_reason == "resource_reservation_capacity"

        await network.release()
        assert await disk.try_acquire() is True
        await disk.release()

    asyncio.run(exercise())
    assert redis.values == {}


def test_renewable_heavy_io_lease_excludes_and_releases():
    from app.services.heavy_io import RenewableRedisLease

    redis = _FakeLeaseRedis()

    async def exercise():
        first = RenewableRedisLease(
            ["lock:heavy-io", "lock:source"],
            workload="download",
            owner="one",
            redis_client=redis,
        )
        second = RenewableRedisLease(
            ["lock:heavy-io"],
            workload="import",
            owner="two",
            redis_client=redis,
        )
        assert await first.try_acquire() is True
        assert await second.try_acquire() is False
        await first.release()
        assert await second.try_acquire() is True
        await second.release()

    asyncio.run(exercise())
    assert redis.values == {}


def test_renewal_thread_runs_while_event_loop_is_blocked():
    from app.services.heavy_io import RenewableRedisLease

    class Redis(_FakeLeaseRedis):
        def __init__(self):
            super().__init__()
            self.renewals = 0

        def eval(self, script, _keys, key, token, *args):
            if "expire" in script:
                self.renewals += 1
            return super().eval(script, _keys, key, token, *args)

    redis = Redis()

    async def exercise():
        lease = RenewableRedisLease(
            ["lock:heavy-io"],
            workload="download",
            owner="blocking-loop",
            redis_client=redis,
        )
        assert await lease.try_acquire() is True
        lease.renew_seconds = 0.01
        lease.start_renewal()

        # Deliberately starve asyncio exactly like download.py's synchronous
        # subprocess polling.  A same-loop renewal task would never run here.
        deadline = time.monotonic() + 0.5
        while redis.renewals == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert redis.renewals > 0
        await lease.release()

    asyncio.run(exercise())


def test_local_heavy_io_flock_is_process_lifetime_mutex(tmp_path):
    from app.services.heavy_io import LocalHeavyIOLock

    path = tmp_path / "heavy-io.lock"
    first = LocalHeavyIOLock(path)
    second = LocalHeavyIOLock(path)
    assert first.try_acquire() is True
    assert second.try_acquire() is False
    first.release()
    assert second.try_acquire() is True
    second.release()


def test_profile_conflicts_replace_the_single_global_lock(monkeypatch, tmp_path):
    from app.services import heavy_io

    monkeypatch.setenv("HEAVY_IO_LOCK_PATH", str(tmp_path / "heavy-io.lock"))
    assert heavy_io.workload_conflict_group("download_network") is None
    assert heavy_io.workload_conflict_group("import_db") == "import-db"
    assert heavy_io.workload_conflict_group("image_derive") == "media"
    assert heavy_io.workload_conflict_group("video_derive") == "media"
    assert heavy_io.workload_conflict_group("backup") == "maintenance"

    import_lock = heavy_io.local_lock_for_workload("import_db")
    search_lock = heavy_io.local_lock_for_workload("search_index")
    maintenance_lock = heavy_io.local_lock_for_workload("backup")
    assert import_lock.path.name == "profile-import-db.lock"
    assert search_lock.path.name == "profile-search-index.lock"
    assert maintenance_lock.path.name == "heavy-io.lock"

    assert import_lock.try_acquire() is True
    assert search_lock.try_acquire() is True
    assert maintenance_lock.try_acquire() is False
    import_lock.release()
    search_lock.release()
    assert maintenance_lock.try_acquire() is True
    maintenance_lock.release()


def test_late_admission_race_returns_delayed_same_job_retry(monkeypatch):
    from rq import Retry

    from app.services import heavy_io

    monkeypatch.setattr("rq.get_current_job", lambda: SimpleNamespace(id="job-1"))
    result = heavy_io._defer_current_rq_job(heavy_io.HeavyIOUnavailable("redis_lease_busy"))

    assert isinstance(result, Retry)
    assert result.max == 1_000_000
    assert result.intervals == [int(heavy_io.DEFAULT_REQUEUE_SECONDS)]


def test_inherited_flock_makes_redis_lease_best_effort(monkeypatch):
    from app.services import heavy_io

    redis = _FakeLeaseRedis()
    redis.values[heavy_io.HEAVY_IO_LOCK_KEY] = "legacy-owner"

    async def unexpected_pressure_check():
        raise AssertionError("an admitted job must not be rejected by late pressure")

    monkeypatch.setattr(heavy_io, "resource_pressure_paused", unexpected_pressure_check)

    async def exercise():
        heavy_io.mark_worker_flock_inherited(True)
        try:
            async with heavy_io.heavy_io_slot(
                "download",
                "job-1",
                redis_client=redis,
            ) as lease:
                assert lease is None
        finally:
            heavy_io.mark_worker_flock_inherited(False)

    asyncio.run(exercise())


def test_source_lock_key_is_stable_and_does_not_expose_url():
    from app.services.heavy_io import source_lock_key

    source = "https://example.test/private/creator?id=123"
    assert source_lock_key(source) == source_lock_key(source)
    assert source not in source_lock_key(source)


class _QueueRedis:
    def __init__(self, *, used=10, maximum=1000, writable=True):
        self.used = used
        self.maximum = maximum
        self.writable = writable

    def info(self, section):
        assert section == "memory"
        return {"used_memory": self.used, "maxmemory": self.maximum}

    def set(self, *_args, **_kwargs):
        if not self.writable:
            raise RuntimeError("OOM command not allowed when used memory > maxmemory")
        return True

    def delete(self, *_args):
        return 1

    def llen(self, _key):
        return 0


def test_download_queue_gate_returns_queue_saturated(monkeypatch):
    from app.services import backpressure

    redis = _QueueRedis()
    queue_counts = {name: 0 for name in backpressure.DOWNLOAD_QUEUE_NAMES}
    queue_counts["downloads"] = 100

    class Queue:
        def __init__(self, name, connection):
            assert connection is redis
            self.name = name

        @classmethod
        def all(cls, connection):
            assert connection is redis
            return []

        @property
        def count(self):
            return queue_counts[self.name]

        @property
        def intermediate_queue_key(self):
            return f"rq:queue:{self.name}:intermediate"

    class EmptyRegistry:
        def __init__(self, name, connection):
            assert name in backpressure.DOWNLOAD_QUEUE_NAMES
            assert connection is redis

        @property
        def count(self):
            return 0

    monkeypatch.setattr(backpressure, "get_redis", lambda: redis)
    monkeypatch.setattr("rq.Queue", Queue)
    monkeypatch.setattr("rq.registry.ScheduledJobRegistry", EmptyRegistry)
    monkeypatch.setattr("rq.registry.DeferredJobRegistry", EmptyRegistry)
    reason = asyncio.run(backpressure.download_queue_reason())
    assert reason["code"] == "queue_saturated"
    assert reason["queued"] == 100
    assert reason["maximum_queued"] == 100


def test_download_queue_gate_detects_noeviction_write_failure(monkeypatch):
    from app.services import backpressure

    monkeypatch.setattr(backpressure, "get_redis", lambda: _QueueRedis(writable=False))
    reason = asyncio.run(backpressure.download_queue_reason())
    assert reason["code"] == "redis_unwritable"
    assert reason["error_type"] == "RuntimeError"


def test_download_create_maps_queue_saturation_to_429(monkeypatch):
    from app.api.download_jobs import create_job
    from app.services.backpressure import DownloadAdmissionError
    from app.services.download import DownloadService

    async def reject(_self, _data):
        raise DownloadAdmissionError(
            "queue_saturated",
            "Download queue is full",
            status_code=429,
            details={"queued": 100, "maximum_queued": 100},
        )

    monkeypatch.setattr(DownloadService, "create_job", reject)
    data = SimpleNamespace(model_dump=lambda: {
        "subscription_id": uuid4(),
        "source": "pixiv",
        "source_url": "https://www.pixiv.net/users/1",
    })
    with pytest.raises(HTTPException) as caught:
        asyncio.run(create_job(data, db=object()))
    assert caught.value.status_code == 429
    assert caught.value.detail["code"] == "queue_saturated"


def test_subscription_scan_never_vacuums_inline():
    from app.jobs import subscription_sync

    source = inspect.getsource(subscription_sync._sync_subscriptions_locked)
    assert 'execute("VACUUM")' not in source
    assert "ensure_sqlite_maintenance_scheduled" in source


@pytest.mark.parametrize(
    ("weekday", "expected_full"),
    [(0, False), (6, True)],
)
def test_sqlite_maintenance_uses_optimize_daily_and_vacuum_sunday(
    monkeypatch,
    tmp_path,
    weekday,
    expected_full,
):
    from app.jobs import sqlite_maintenance

    redis = SimpleNamespace(delete=lambda *_args: 1)
    calls = []

    async def not_paused():
        return False, {"status": "normal", "reasons": []}

    @asynccontextmanager
    async def acquired(*_args, **_kwargs):
        yield object()

    db_path = tmp_path / "archive-pixiv.sqlite3"
    db_path.write_bytes(b"fixture")
    monkeypatch.setattr(sqlite_maintenance, "get_redis", lambda: redis)
    monkeypatch.setattr(sqlite_maintenance, "resource_pressure_paused", not_paused)
    monkeypatch.setattr(sqlite_maintenance, "_heavy_queues_idle", lambda: (True, {}))
    monkeypatch.setattr(sqlite_maintenance, "try_heavy_io_slot", acquired)
    monkeypatch.setattr(sqlite_maintenance, "_database_paths", lambda: [db_path])
    monkeypatch.setattr(
        sqlite_maintenance,
        "_maintain_database",
        lambda path, *, full_vacuum: calls.append((path, full_vacuum)),
    )
    monkeypatch.setattr(sqlite_maintenance, "ensure_sqlite_maintenance_scheduled", lambda _now: True)

    # 2026-08-03 is Monday; add ``weekday`` days to select Monday/Sunday.
    day = 3 + weekday
    now = datetime(2026, 8, day, 3, 30, tzinfo=ZoneInfo("UTC"))
    result = asyncio.run(sqlite_maintenance.run_sqlite_maintenance_async(now))
    assert result["status"] == "complete"
    assert calls == [(db_path, expected_full)]


def test_media_limits_vips_cache_and_ffmpeg_threads(tmp_path, monkeypatch):
    from app.services import media_assets, thumbnail

    configured = []
    thumbnail._configure_pyvips(SimpleNamespace(cache_set_max_mem=configured.append))
    assert configured == [64 * 1024 * 1024]
    assert thumbnail.os.environ["VIPS_CONCURRENCY"] == "1"

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fixture")
    target = tmp_path / "poster.webp"
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"webp")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(media_assets.subprocess, "run", fake_run)
    assert media_assets._render_video_frame(
        source,
        target,
        seek_seconds=0,
        max_size=400,
        timeout_seconds=5,
    ) is None
    command = commands[0]
    assert command.count("-threads") == 2
    assert "-filter_threads" in command
    assert "-filter_complex_threads" in command
