"""API process budget and singleton maintenance coordination."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

API_WORKER_CONCURRENCY_ENV = "API_WORKER_CONCURRENCY"
API_WORKER_CONCURRENCY_DEFAULT = 1
API_WORKER_CONCURRENCY_MIN = 1
API_WORKER_CONCURRENCY_MAX = 4
API_DB_POOL_SIZE_ENV = "DB_POOL_SIZE"
API_DB_MAX_OVERFLOW_ENV = "DB_MAX_OVERFLOW"
API_MEILI_SEARCH_CONCURRENCY_ENV = "MEILI_SEARCH_CONCURRENCY"
API_THREAD_POOL_SIZE_ENV = "API_THREAD_POOL_SIZE"
API_MAINTENANCE_LEASE_KEY = "api:maintenance:leader:v1"
API_MAINTENANCE_LEASE_SECONDS = 20
API_MAINTENANCE_RENEW_SECONDS = 5
API_MAINTENANCE_POLL_SECONDS = 1
API_HEALTH_SNAPSHOT_KEY = "api:health:snapshot:v1"
API_HEALTH_SNAPSHOT_TTL_SECONDS = 45
API_FOREGROUND_SAMPLE_PREFIX = "api:foreground-latency:v1:"
API_FOREGROUND_AGGREGATE_KEY = "api:foreground-latency:aggregate:v1"
API_FOREGROUND_SAMPLE_TTL_SECONDS = 30

logger = logging.getLogger(__name__)

_RELEASE_LEASE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""

_FENCED_HEALTH_SET = """
if redis.call("get", KEYS[1]) ~= ARGV[1] then
  return 0
end
redis.call("set", KEYS[2], ARGV[2], "ex", ARGV[3])
return 1
"""

_FENCED_FOREGROUND_SET = """
if redis.call("get", KEYS[1]) ~= ARGV[1] then
  return 0
end
local current = redis.call("get", KEYS[2])
if current then
  local ok, payload = pcall(cjson.decode, current)
  if ok and type(payload) == "table" then
    local current_epoch = tonumber(payload["sample_generation"] or 0)
    if current_epoch > tonumber(ARGV[3]) then
      return 2
    end
  end
end
redis.call("set", KEYS[2], ARGV[2], "ex", ARGV[4])
return 1
"""

_RENEW_LEASE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  redis.call("expire", KEYS[1], ARGV[2])
  return 1
end
return 0
"""


def _redis_client():
    """Import Redis only after the launcher has split process budgets."""
    from app.services.redis_client import get_redis

    return get_redis()


def resolve_api_worker_concurrency(raw_value: str | int | None) -> int:
    raw = (
        os.environ.get(API_WORKER_CONCURRENCY_ENV, str(API_WORKER_CONCURRENCY_DEFAULT))
        if raw_value is None
        else raw_value
    )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{API_WORKER_CONCURRENCY_ENV} must be an integer between "
            f"{API_WORKER_CONCURRENCY_MIN} and {API_WORKER_CONCURRENCY_MAX}; got {raw!r}"
        ) from exc
    if not API_WORKER_CONCURRENCY_MIN <= value <= API_WORKER_CONCURRENCY_MAX:
        raise ValueError(
            f"{API_WORKER_CONCURRENCY_ENV} must be between "
            f"{API_WORKER_CONCURRENCY_MIN} and {API_WORKER_CONCURRENCY_MAX}; got {value}"
        )
    return value


def _positive_int(value: str | int | None, *, name: str) -> int:
    try:
        result = int(value) if value is not None else 0
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer; got {value!r}") from exc
    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer; got {value!r}")
    return result


def _floor_budget(total: int, workers: int, *, name: str) -> int:
    per_process = total // workers
    if per_process < 1:
        raise ValueError(f"{name}={total} cannot provide one slot per API process")
    return per_process


def allocate_api_database_budget(
    total_pool_size: str | int | None,
    total_max_overflow: str | int | None,
    workers: str | int | None,
) -> tuple[int, int]:
    count = resolve_api_worker_concurrency(workers)
    pool = _floor_budget(
        _positive_int(total_pool_size, name=API_DB_POOL_SIZE_ENV),
        count,
        name=API_DB_POOL_SIZE_ENV,
    )
    overflow_total = _positive_int(total_max_overflow, name=API_DB_MAX_OVERFLOW_ENV)
    overflow = overflow_total // count
    return pool, overflow


def allocate_api_thread_budget(
    total_thread_pool_size: str | int | None,
    workers: str | int | None,
) -> int:
    count = resolve_api_worker_concurrency(workers)
    return _floor_budget(
        _positive_int(total_thread_pool_size, name=API_THREAD_POOL_SIZE_ENV),
        count,
        name=API_THREAD_POOL_SIZE_ENV,
    )


def configure_api_thread_budget(max_workers: int) -> ThreadPoolExecutor:
    """Bound asyncio.to_thread and AnyIO worker threads for this API process."""
    if int(max_workers) < 1:
        raise ValueError("API_THREAD_POOL_SIZE must be at least 1")
    executor = ThreadPoolExecutor(
        max_workers=int(max_workers),
        thread_name_prefix="auto-gallery-api",
    )
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        from anyio.to_thread import current_default_thread_limiter
    except ImportError as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        raise RuntimeError(
            "AnyIO is required to configure the API thread limiter"
        ) from exc
    try:
        limiter = current_default_thread_limiter()
    except RuntimeError:
        # AnyIO can only resolve its backend from an active async context.
        # This is a supported pure-asyncio unit-test/import path; the normal
        # Uvicorn lifespan has an active asyncio backend.
        logger.warning(
            "AnyIO default thread limiter was unavailable during API startup; "
            "asyncio.to_thread remains bounded",
            exc_info=True,
        )
    else:
        try:
            limiter.total_tokens = int(max_workers)
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    return executor


def allocate_api_meili_budget(
    total_search_concurrency: str | int | None,
    workers: str | int | None,
) -> int:
    count = resolve_api_worker_concurrency(workers)
    return _floor_budget(
        _positive_int(
            total_search_concurrency,
            name=API_MEILI_SEARCH_CONCURRENCY_ENV,
        ),
        count,
        name=API_MEILI_SEARCH_CONCURRENCY_ENV,
    )


def prepare_api_environment(workers: str | int | None = None) -> int:
    """Split total budgets exactly once, before Uvicorn imports app.main."""
    count = resolve_api_worker_concurrency(
        os.environ.get(API_WORKER_CONCURRENCY_ENV) if workers is None else workers
    )
    marker = os.environ.get("_AUTO_GALLERY_API_ENV_PREPARED")
    if marker:
        prepared_for = resolve_api_worker_concurrency(marker)
        if prepared_for != count:
            raise RuntimeError(
                f"API environment was already prepared for {prepared_for} workers"
            )
        return count

    pool, overflow = allocate_api_database_budget(
        os.environ.get(API_DB_POOL_SIZE_ENV, "8"),
        os.environ.get(API_DB_MAX_OVERFLOW_ENV, "4"),
        count,
    )
    meili = allocate_api_meili_budget(
        os.environ.get(API_MEILI_SEARCH_CONCURRENCY_ENV, "4"),
        count,
    )
    thread_pool = allocate_api_thread_budget(
        os.environ.get(API_THREAD_POOL_SIZE_ENV, "16"),
        count,
    )
    os.environ[API_WORKER_CONCURRENCY_ENV] = str(count)
    os.environ[API_DB_POOL_SIZE_ENV] = str(pool)
    os.environ[API_DB_MAX_OVERFLOW_ENV] = str(overflow)
    os.environ[API_MEILI_SEARCH_CONCURRENCY_ENV] = str(meili)
    os.environ[API_THREAD_POOL_SIZE_ENV] = str(thread_pool)
    os.environ["_AUTO_GALLERY_API_ENV_PREPARED"] = str(count)
    return count


class ApiMaintenanceCoordinator:
    """Elect and supervise one process running singleton API maintenance."""

    def __init__(
        self,
        *,
        key: str = API_MAINTENANCE_LEASE_KEY,
        enabled: bool = True,
    ) -> None:
        self.key = key
        self.enabled = enabled
        self.token = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.instance_id = uuid.uuid4().hex
        self.is_owner = False
        self._stop = asyncio.Event()
        self._foreground_signature: tuple[tuple[str, int], ...] | None = None
        self._foreground_aggregation_epoch: int | None = None

    async def _acquire_or_renew(self) -> bool:
        if not self.enabled:
            self.is_owner = True
            return True
        try:
            client = _redis_client()
            if self.is_owner:
                owned = bool(
                    await asyncio.to_thread(
                        lambda: client.eval(
                            _RENEW_LEASE,
                            1,
                            self.key,
                            self.token,
                            API_MAINTENANCE_LEASE_SECONDS,
                        )
                    )
                )
            else:
                candidate_token = f"{os.getpid()}:{uuid.uuid4().hex}"
                owned = bool(
                    await asyncio.to_thread(
                        lambda: client.set(
                            self.key,
                            candidate_token,
                            nx=True,
                            ex=API_MAINTENANCE_LEASE_SECONDS,
                        )
                    )
                )
                if owned:
                    self.token = candidate_token
            self.is_owner = owned
            return owned
        except Exception:
            self.is_owner = False
            return False

    async def release(self, lease_token: str | None = None) -> None:
        token = lease_token or self.token
        if not self.enabled or not self.is_owner:
            self.is_owner = False
            return
        try:
            client = _redis_client()
            await asyncio.to_thread(
                lambda: client.eval(_RELEASE_LEASE, 1, self.key, token)
            )
        except Exception:
            logger.debug("API maintenance lease release failed", exc_info=True)
        finally:
            if token == self.token:
                self.is_owner = False

    async def stop(self) -> None:
        self._stop.set()

    async def _cancel_group(self, tasks: Sequence[asyncio.Task[Any]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(
        self,
        factories: Sequence[Callable[[], Awaitable[Any]]],
    ) -> None:
        """Run a maintenance group, cancelling it immediately on lease loss."""
        if not self.enabled:
            tasks = [asyncio.create_task(factory()) for factory in factories]
            try:
                await asyncio.gather(*tasks)
            finally:
                await self._cancel_group(tasks)
            return

        while not self._stop.is_set():
            if not await self._acquire_or_renew():
                try:
                    await asyncio.wait_for(self._stop.wait(), API_MAINTENANCE_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue

            lease_token = self.token
            group = [asyncio.create_task(factory()) for factory in factories]
            lost = asyncio.Event()

            async def renew_loop() -> None:
                while not self._stop.is_set():
                    await asyncio.sleep(API_MAINTENANCE_RENEW_SECONDS)
                    if not await self._acquire_or_renew():
                        lost.set()
                        return

            renew_task = asyncio.create_task(renew_loop())
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                done, _ = await asyncio.wait(
                    [*group, renew_task, stop_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._stop.is_set() or stop_task in done:
                    return
                if lost.is_set() or renew_task in done:
                    logger.warning("API maintenance lease lost; cancelling maintenance group")
                else:
                    failures = [task for task in done if task in group and task.exception()]
                    if failures:
                        logger.error(
                            "API maintenance task failed; restarting group: %r",
                            failures[0].exception(),
                        )
            finally:
                await self._cancel_group(group)
                await self._cancel_group([renew_task, stop_task])
                await self.release(lease_token)

    async def publish_health_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not self.enabled:
            return
        lease_token = self.token
        try:
            client = _redis_client()
            payload = json.dumps(snapshot, separators=(",", ":"))
            await asyncio.to_thread(
                lambda: client.eval(
                    _FENCED_HEALTH_SET,
                    2,
                    self.key,
                    API_HEALTH_SNAPSHOT_KEY,
                    lease_token,
                    payload,
                    API_HEALTH_SNAPSHOT_TTL_SECONDS,
                )
            )
        except Exception:
            logger.debug("Shared health snapshot publish failed", exc_info=True)

    async def read_health_snapshot(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = await asyncio.to_thread(lambda: _redis_client().get(API_HEALTH_SNAPSHOT_KEY))
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            value = json.loads(raw) if raw else None
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    async def publish_foreground_sample(self, snapshot: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            client = _redis_client()
            key = f"{API_FOREGROUND_SAMPLE_PREFIX}{self.instance_id}"
            payload = dict(snapshot)
            payload["instance_id"] = self.instance_id
            await asyncio.to_thread(
                lambda: client.set(
                    key,
                    json.dumps(payload, separators=(",", ":")),
                    ex=API_FOREGROUND_SAMPLE_TTL_SECONDS,
                )
            )
        except Exception:
            logger.debug("Foreground latency sample publish failed", exc_info=True)

    def is_lease_owner_sync(self, lease_token: str) -> bool:
        if not self.enabled:
            return True
        try:
            raw = _redis_client().get(self.key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return raw == lease_token
        except Exception:
            return False

    def is_current_owner_sync(self) -> bool:
        return self.is_lease_owner_sync(self.token)

    def read_foreground_aggregate_sync(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            raw = _redis_client().get(API_FOREGROUND_AGGREGATE_KEY)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            value = json.loads(raw) if raw else None
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    async def aggregate_foreground_samples(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            client = _redis_client()
            lease_token = self.token

            def read(token: str) -> dict[str, Any] | None:
                samples = []
                for key in client.scan_iter(match=f"{API_FOREGROUND_SAMPLE_PREFIX}*"):
                    raw = client.get(key)
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        value = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        samples.append(value)
                if not samples:
                    return None
                counts = [int(v.get("sample_count") or 0) for v in samples]
                sample_count = sum(counts)
                contributors = {
                    str(v.get("instance_id")): int(v.get("sample_generation") or 0)
                    for v in samples
                    if v.get("instance_id") and int(v.get("sample_count") or 0) > 0
                }
                previous = None
                raw_previous = client.get(API_FOREGROUND_AGGREGATE_KEY)
                if isinstance(raw_previous, bytes):
                    raw_previous = raw_previous.decode("utf-8")
                try:
                    previous = json.loads(raw_previous) if raw_previous else None
                except (TypeError, ValueError):
                    previous = None
                if not contributors or sample_count <= 0:
                    return previous if isinstance(previous, dict) else None
                # Read the shared pressure generation on every cycle, not
                # only after this process starts, so a returning owner cannot
                # publish an epoch behind a newer owner.
                from app.services.resource_pressure_snapshot import PRESSURE_SNAPSHOT_KEY

                raw_pressure = client.get(PRESSURE_SNAPSHOT_KEY)
                if isinstance(raw_pressure, bytes):
                    raw_pressure = raw_pressure.decode("utf-8")
                try:
                    pressure = json.loads(raw_pressure) if raw_pressure else {}
                except (TypeError, ValueError):
                    pressure = {}
                shared_epoch = max(
                    int((previous or {}).get("sample_generation") or 0),
                    int(((pressure or {}).get("foreground") or {}).get("sample_generation") or 0),
                )
                if self._foreground_aggregation_epoch is None:
                    self._foreground_aggregation_epoch = shared_epoch
                    previous_contributors = (
                        (previous or {}).get("contributors")
                        or {}
                    )
                    if not previous_contributors:
                        previous_contributors = (
                            ((pressure or {}).get("foreground") or {}).get("contributors")
                            or {}
                        )
                    if previous_contributors:
                        self._foreground_signature = tuple(
                            sorted(
                                (str(key), int(value))
                                for key, value in previous_contributors.items()
                            )
                        )
                self._foreground_aggregation_epoch = max(
                    self._foreground_aggregation_epoch or 0,
                    shared_epoch,
                )
                previous_signature = dict(self._foreground_signature or ())
                request_generation_changed = any(
                    previous_signature.get(instance_id) != generation
                    for instance_id, generation in contributors.items()
                )
                # Removing an expired sample or observing a zero-sample restart
                # is not a new request. Only a contributor with real samples
                # and a changed generation advances the shared epoch.
                if request_generation_changed:
                    self._foreground_aggregation_epoch += 1
                    previous_signature.update(contributors)
                self._foreground_signature = tuple(sorted(previous_signature.items()))

                p95_values = [float(v["p95_ms"]) for v in samples if v.get("p95_ms") is not None]
                aggregate = {
                    "p95_ms": max(p95_values) if p95_values else None,
                    "sample_count": sample_count,
                    "process_count": len(samples),
                    "window_seconds": max(int(v.get("window_seconds") or 0) for v in samples),
                    "sample_generation": self._foreground_aggregation_epoch,
                    "contributors": dict(contributors),
                }
                encoded = json.dumps(aggregate, separators=(",", ":"))
                written = client.eval(
                    _FENCED_FOREGROUND_SET,
                    2,
                    self.key,
                    API_FOREGROUND_AGGREGATE_KEY,
                    token,
                    encoded,
                    self._foreground_aggregation_epoch,
                    API_FOREGROUND_SAMPLE_TTL_SECONDS,
                )
                if not written:
                    raw_current = client.get(API_FOREGROUND_AGGREGATE_KEY)
                    if isinstance(raw_current, bytes):
                        raw_current = raw_current.decode("utf-8")
                    try:
                        current = json.loads(raw_current) if raw_current else None
                    except (TypeError, ValueError):
                        current = None
                    return current if isinstance(current, dict) else None
                return aggregate
            return await asyncio.to_thread(read, lease_token)
        except Exception:
            logger.debug("Foreground latency aggregation failed", exc_info=True)
            return None

    @asynccontextmanager
    async def cycle(self):
        """Compatibility helper for callers that need one election cycle."""
        yield await self._acquire_or_renew()


def resource_pressure_owner_kwargs(coordinator: ApiMaintenanceCoordinator) -> dict[str, Any]:
    """Return fencing callbacks only for the coordinated multi-process path."""
    if not coordinator.enabled:
        return {}
    lease_token = coordinator.token
    return {
        "ownership_guard": lambda: coordinator.is_lease_owner_sync(lease_token),
        "ownership_key": coordinator.key,
        "ownership_token": lease_token,
    }
