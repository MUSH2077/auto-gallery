import asyncio
import os
import sys
import threading
import types

import pytest

import api_entrypoint
from app import api_runtime


async def wait_thread_event(event, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return event.is_set()


def test_api_worker_defaults_to_one(monkeypatch):
    monkeypatch.delenv("API_WORKER_CONCURRENCY", raising=False)
    assert api_runtime.resolve_api_worker_concurrency(None) == 1


@pytest.mark.parametrize("value", ["0", "5", "bad", ""])
def test_api_worker_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="API_WORKER_CONCURRENCY"):
        api_runtime.resolve_api_worker_concurrency(value)


@pytest.mark.parametrize(
    ("workers", "expected"),
    [(1, (8, 4)), (2, (4, 2)), (4, (2, 1))],
)
def test_database_budget_is_split_without_multiplying(workers, expected):
    assert api_runtime.allocate_api_database_budget(8, 4, workers) == expected


def test_database_budget_rejects_zero_connection_process(monkeypatch):
    with pytest.raises(ValueError, match="DB_POOL_SIZE"):
        api_runtime.allocate_api_database_budget(3, 4, 4)


def test_prepare_environment_normalizes_before_app_import(monkeypatch):
    monkeypatch.delenv("_AUTO_GALLERY_API_ENV_PREPARED", raising=False)
    monkeypatch.setenv("API_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("DB_POOL_SIZE", "8")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "4")
    monkeypatch.setenv("MEILI_SEARCH_CONCURRENCY", "4")
    monkeypatch.setenv("API_THREAD_POOL_SIZE", "16")

    assert api_runtime.prepare_api_environment() == 4
    assert os.environ["API_WORKER_CONCURRENCY"] == "4"
    assert os.environ["DB_POOL_SIZE"] == "2"
    assert os.environ["DB_MAX_OVERFLOW"] == "1"
    assert os.environ["MEILI_SEARCH_CONCURRENCY"] == "1"
    assert os.environ["API_THREAD_POOL_SIZE"] == "4"
    # Calling the launcher hook again in a child-like environment is a no-op.
    assert api_runtime.prepare_api_environment() == 4
    assert os.environ["DB_POOL_SIZE"] == "2"


@pytest.mark.parametrize(
    ("workers", "expected_db", "expected_meili"),
    [(3, (2, 1), 1), (4, (2, 1), 1)],
)
def test_non_divisible_budgets_use_floor_without_exceeding_totals(
    workers, expected_db, expected_meili
):
    assert api_runtime.allocate_api_database_budget(8, 4, workers) == expected_db
    assert api_runtime.allocate_api_meili_budget(4, workers) == expected_meili
    assert api_runtime.allocate_api_thread_budget(16, workers) == max(1, 16 // workers)


def test_old_owner_cannot_renew_a_replaced_lease(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: redis)
    first = api_runtime.ApiMaintenanceCoordinator()
    second = api_runtime.ApiMaintenanceCoordinator()

    async def scenario():
        assert await first._acquire_or_renew()
        redis.value = second.token
        assert not await first._acquire_or_renew()
        assert redis.value == second.token

    asyncio.run(scenario())


class FakeRedis:
    def __init__(self):
        self.value = None

    def set(self, key, token, *, nx=False, xx=False, ex=None):
        if nx and self.value is not None:
            return False
        if xx and self.value is None:
            return False
        self.value = token
        return True

    def eval(self, script, _numkeys, _key, token, *args):
        if "expire" in script:
            return int(self.value == token)
        if self.value == token:
            self.value = None
            return 1
        return 0


def test_maintenance_leader_takeover_and_release(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: redis)
    first = api_runtime.ApiMaintenanceCoordinator()
    second = api_runtime.ApiMaintenanceCoordinator()

    async def scenario():
        assert await first._acquire_or_renew()
        assert not await second._acquire_or_renew()
        await first.release()
        assert await second._acquire_or_renew()

    asyncio.run(scenario())


def test_maintenance_owner_stops_when_redis_renewal_fails(monkeypatch):
    class BrokenRedis:
        def set(self, *args, **kwargs):
            raise RuntimeError("redis unavailable")

        def eval(self, *args, **kwargs):
            raise RuntimeError("redis unavailable")

    coordinator = api_runtime.ApiMaintenanceCoordinator()
    coordinator.is_owner = True
    coordinator._last_renewed = 0
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: BrokenRedis())

    assert asyncio.run(coordinator._acquire_or_renew()) is False
    assert coordinator.is_owner is False


def test_entrypoint_uses_native_uvicorn_worker_supervisor(monkeypatch):
    calls = []
    fake_uvicorn = types.SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(api_entrypoint, "prepare_api_environment", lambda: 4)

    api_entrypoint.main()

    assert calls == [
        (("app.main:app",), {"host": "0.0.0.0", "port": 8000, "workers": 4})
    ]


def test_maintenance_group_is_cancelled_when_renewal_loses_lease(monkeypatch):
    class SwitchingRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.renew_allowed = True

        def eval(self, script, numkeys, key, token, *args):
            if "expire" in script:
                return int(self.renew_allowed and self.value == token)
            return super().eval(script, numkeys, key, token, *args)

    redis = SwitchingRedis()
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: redis)
    monkeypatch.setattr(api_runtime, "API_MAINTENANCE_RENEW_SECONDS", 0.01)
    coordinator = api_runtime.ApiMaintenanceCoordinator()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_task():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def scenario():
        run_task = asyncio.create_task(coordinator.run([lambda: long_task()]))
        await asyncio.wait_for(started.wait(), 1)
        redis.renew_allowed = False
        await asyncio.wait_for(cancelled.wait(), 1)
        await coordinator.stop()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    asyncio.run(scenario())


def test_foreground_aggregate_generation_changes_only_with_real_samples(monkeypatch):
    class SampleRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, **kwargs):
            if kwargs.get("nx") and key in self.data:
                return False
            self.data[key] = value
            return True

        def get(self, key):
            return self.data.get(key)

        def scan_iter(self, match):
            prefix = match.rstrip("*")
            return [key for key in self.data if key.startswith(prefix)]

        def eval(self, script, numkeys, *args):
            if "sample_generation" in script:
                owner_key, aggregate_key, token, payload, candidate, _ttl = args
                if self.data.get(owner_key) != token:
                    return 0
                current = self.data.get(aggregate_key)
                if current:
                    import json
                    current_epoch = int(json.loads(current).get("sample_generation") or 0)
                    if current_epoch > int(candidate):
                        return 2
                self.data[aggregate_key] = payload
                return 1
            raise AssertionError("unexpected script")

    redis = SampleRedis()
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: redis)
    coordinator = api_runtime.ApiMaintenanceCoordinator()
    redis.data[coordinator.key] = coordinator.token
    coordinator.is_owner = True

    async def scenario():
        await coordinator.publish_foreground_sample(
            {"p95_ms": 100, "sample_count": 2, "sample_generation": 7, "window_seconds": 300}
        )
        first = await coordinator.aggregate_foreground_samples()
        second = await coordinator.aggregate_foreground_samples()
        assert first["sample_generation"] == 1
        assert second["sample_generation"] == 1
        await coordinator.publish_foreground_sample(
            {"p95_ms": 120, "sample_count": 3, "sample_generation": 8, "window_seconds": 300}
        )
        third = await coordinator.aggregate_foreground_samples()
        assert third["sample_generation"] == 2
        restarted = api_runtime.ApiMaintenanceCoordinator()
        stable = await restarted.aggregate_foreground_samples()
        assert stable["sample_generation"] == 2

        # A -> B -> A takeover: A's late aggregate is rejected by the
        # current owner token and cannot overwrite B's newer epoch.
        successor = api_runtime.ApiMaintenanceCoordinator()
        redis.data[coordinator.key] = successor.token
        successor.is_owner = True
        await successor.publish_foreground_sample(
            {"p95_ms": 130, "sample_count": 4, "sample_generation": 9, "window_seconds": 300}
        )
        newer = await successor.aggregate_foreground_samples()
        assert newer["sample_generation"] == 3
        # The old A owner is now late while B owns the lease; its Lua write
        # must be rejected and B's epoch must remain intact.
        redis.data[coordinator.key] = successor.token
        late = await coordinator.aggregate_foreground_samples()
        assert late["sample_generation"] == newer["sample_generation"]
        # A can take over afterward, but it must start from the shared epoch.
        redis.data[coordinator.key] = coordinator.token
        reacquired = await coordinator.aggregate_foreground_samples()
        assert reacquired["sample_generation"] >= newer["sample_generation"]

    asyncio.run(scenario())


def test_foreground_generation_ignores_zero_sample_restart_and_expiry(monkeypatch):
    class SampleRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, **kwargs):
            self.data[key] = value
            return True

        def get(self, key):
            return self.data.get(key)

        def scan_iter(self, match):
            prefix = match.rstrip("*")
            return [key for key in self.data if key.startswith(prefix)]

        def eval(self, script, numkeys, *args):
            owner_key, aggregate_key, token, payload, _epoch, _ttl = args
            if self.data.get(owner_key) != token:
                return 0
            self.data[aggregate_key] = payload
            return 1

    redis = SampleRedis()
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: redis)
    coordinator = api_runtime.ApiMaintenanceCoordinator()
    redis.data[coordinator.key] = coordinator.token
    coordinator.is_owner = True

    async def scenario():
        key = f"{api_runtime.API_FOREGROUND_SAMPLE_PREFIX}{coordinator.instance_id}"
        redis.data[key] = (
            '{"instance_id":"' + coordinator.instance_id
            + '","p95_ms":100,"sample_count":2,"sample_generation":7,"window_seconds":300}'
        )
        first = await coordinator.aggregate_foreground_samples()
        assert first["sample_generation"] == 1

        # A restarted publisher with no requests must not advance the epoch.
        redis.data[key] = (
            '{"instance_id":"' + coordinator.instance_id
            + '","p95_ms":null,"sample_count":0,"sample_generation":8,"window_seconds":300}'
        )
        zero = await coordinator.aggregate_foreground_samples()
        assert zero["sample_generation"] == first["sample_generation"]

        # Expiring the old sample also must not advance the epoch.
        del redis.data[key]
        expired = await coordinator.aggregate_foreground_samples()
        assert expired is None
        assert coordinator._foreground_aggregation_epoch == first["sample_generation"]

    asyncio.run(scenario())


def test_late_health_publish_cannot_cross_owner_generation(monkeypatch):
    class BlockingHealthRedis:
        def __init__(self):
            self.data = {}
            self.entered = threading.Event()
            self.release = threading.Event()

        def get(self, key):
            return self.data.get(key)

        def set(self, key, value, **kwargs):
            if kwargs.get("nx") and key in self.data:
                return False
            self.data[key] = value
            return True

        def eval(self, script, numkeys, owner_key, snapshot_key, token, payload, _ttl):
            if snapshot_key == api_runtime.API_HEALTH_SNAPSHOT_KEY:
                self.entered.set()
                self.release.wait(1)
                if self.data.get(owner_key) != token:
                    return 0
                self.data[snapshot_key] = payload
                return 1
            raise AssertionError("unexpected script")

    redis = BlockingHealthRedis()
    monkeypatch.setattr(api_runtime, "_redis_client", lambda: redis)
    first = api_runtime.ApiMaintenanceCoordinator()
    redis.data[first.key] = first.token
    first.is_owner = True

    async def scenario():
        late = asyncio.create_task(first.publish_health_snapshot({"owner": "old"}))
        assert await wait_thread_event(redis.entered)

        successor = api_runtime.ApiMaintenanceCoordinator()
        redis.data[first.key] = successor.token
        first.is_owner = False
        redis.data.pop(first.key)
        assert await first._acquire_or_renew()
        assert first.token != successor.token
        new_token = first.token

        # The blocked call captured first's previous token. Even after A
        # reacquires the lease with a new token, the old write is rejected.
        redis.data[first.key] = new_token
        redis.release.set()
        await late
        assert api_runtime.API_HEALTH_SNAPSHOT_KEY not in redis.data

        await first.publish_health_snapshot({"owner": "new"})
        assert '"new"' in redis.data[api_runtime.API_HEALTH_SNAPSHOT_KEY]

    asyncio.run(scenario())


def test_resource_pressure_fence_is_only_enabled_for_multi_process_api():
    single = api_runtime.ApiMaintenanceCoordinator(enabled=False)
    assert api_runtime.resource_pressure_owner_kwargs(single) == {}

    multi = api_runtime.ApiMaintenanceCoordinator(enabled=True)
    kwargs = api_runtime.resource_pressure_owner_kwargs(multi)
    assert kwargs["ownership_key"] == multi.key
    assert kwargs["ownership_token"] == multi.token
    assert kwargs["ownership_token"] == multi.token
    assert kwargs["ownership_guard"]() is False


def test_old_resource_thread_cannot_write_after_new_lease():
    class LeaseRedis:
        def __init__(self):
            self.data = {}

        def get(self, key):
            return self.data.get(key)

        def set(self, key, value, **kwargs):
            if kwargs.get("nx") and key in self.data:
                return False
            self.data[key] = value
            return True

        def eval(self, _script, _numkeys, key, token, *args):
            if self.data.get(key) == token:
                self.data.pop(key, None)
                return 1
            return 0

    redis = LeaseRedis()
    original = api_runtime._redis_client
    api_runtime._redis_client = lambda: redis
    try:
        coordinator = api_runtime.ApiMaintenanceCoordinator()
        old_token = coordinator.token
        redis.data[coordinator.key] = old_token
        coordinator.is_owner = True
        kwargs = api_runtime.resource_pressure_owner_kwargs(coordinator)

        started = threading.Event()
        release = threading.Event()

        def blocked_resource_thread():
            started.set()
            release.wait(1)
            return kwargs["ownership_guard"](), kwargs["ownership_token"]

        async def scenario():
            thread = asyncio.create_task(asyncio.to_thread(blocked_resource_thread))
            assert await wait_thread_event(started)
            successor = api_runtime.ApiMaintenanceCoordinator()
            redis.data[coordinator.key] = successor.token
            coordinator.is_owner = False
            redis.data.pop(coordinator.key)
            assert await coordinator._acquire_or_renew()
            assert coordinator.token != old_token
            new_token = coordinator.token
            await coordinator.release(old_token)
            assert redis.data[coordinator.key] == new_token
            release.set()
            guard, captured = await thread
            assert guard is False
            assert captured == old_token

        asyncio.run(scenario())
    finally:
        api_runtime._redis_client = original


def test_old_aggregate_thread_is_rejected_after_b_takeover_and_a_reacquire():
    class BlockingAggregateRedis:
        def __init__(self):
            self.data = {}
            self.entered = threading.Event()
            self.release = threading.Event()

        def get(self, key):
            return self.data.get(key)

        def set(self, key, value, **kwargs):
            if kwargs.get("nx") and key in self.data:
                return False
            self.data[key] = value
            return True

        def scan_iter(self, match):
            prefix = match.rstrip("*")
            return [key for key in self.data if key.startswith(prefix)]

        def eval(self, script, numkeys, *args):
            if "sample_generation" not in script:
                raise AssertionError("unexpected script")
            self.entered.set()
            self.release.wait(1)
            owner_key, aggregate_key, token, payload, _epoch, _ttl = args
            if self.data.get(owner_key) != token:
                return 0
            self.data[aggregate_key] = payload
            return 1

    redis = BlockingAggregateRedis()
    original = api_runtime._redis_client
    api_runtime._redis_client = lambda: redis
    try:
        coordinator = api_runtime.ApiMaintenanceCoordinator()
        old_token = coordinator.token
        redis.data[coordinator.key] = old_token
        coordinator.is_owner = True

        async def scenario():
            await coordinator.publish_foreground_sample(
                {"p95_ms": 100, "sample_count": 1, "sample_generation": 1, "window_seconds": 300}
            )
            aggregate = asyncio.create_task(coordinator.aggregate_foreground_samples())
            assert await wait_thread_event(redis.entered)

            successor = api_runtime.ApiMaintenanceCoordinator()
            redis.data[coordinator.key] = successor.token
            coordinator.is_owner = False
            redis.data.pop(coordinator.key)
            assert await coordinator._acquire_or_renew()
            assert coordinator.token != old_token

            redis.release.set()
            assert await aggregate is None
            assert api_runtime.API_FOREGROUND_AGGREGATE_KEY not in redis.data

        asyncio.run(scenario())
    finally:
        api_runtime._redis_client = original


@pytest.mark.parametrize("workers, expected", [(1, 16), (2, 8), (3, 5), (4, 4)])
def test_thread_budget_floor_and_minimum(workers, expected):
    assert api_runtime.allocate_api_thread_budget(16, workers) == expected


def test_thread_budget_rejects_less_than_one_per_process():
    with pytest.raises(ValueError, match="API_THREAD_POOL_SIZE"):
        api_runtime.allocate_api_thread_budget(3, 4)


def test_asyncio_to_thread_jobs_are_bounded_by_default_executor():
    async def scenario():
        executor = api_runtime.configure_api_thread_budget(2)
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        count = 0

        def blocking():
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    started.set()
            release.wait(1)

        try:
            jobs = [asyncio.create_task(asyncio.to_thread(blocking)) for _ in range(3)]
            assert await wait_thread_event(started)
            assert count == 2
            release.set()
            await asyncio.gather(*jobs)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    asyncio.run(scenario())


def test_anyio_thread_jobs_are_bounded_by_default_limiter():
    import anyio

    async def scenario():
        executor = api_runtime.configure_api_thread_budget(2)
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        count = 0

        def blocking():
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    started.set()
            release.wait(1)

        try:
            jobs = [asyncio.create_task(anyio.to_thread.run_sync(blocking)) for _ in range(3)]
            assert await wait_thread_event(started)
            assert count == 2
            release.set()
            await asyncio.gather(*jobs)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    anyio.run(scenario, backend="asyncio")
