"""Profile-scoped admission for bounded background slices.

Ordinary profiles take a shared POSIX maintenance barrier and a bounded Redis
reservation.  The first NAS rollout intentionally permits one disk slice plus
one network download at a time; unlike the former monolithic lock, foreground
and control work continue and the disk token is released every 20 seconds (or
one bounded unit).  Maintenance takes the same POSIX barrier exclusively.
Redis leases provide cross-process visibility and an atomic RAM reservation;
POSIX locks on the shared NAS volume remain authoritative if Redis stalls.
Downloads may run to completion and only serialize the same source archive.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import hashlib
import inspect
import json
import logging
import os
import random
import threading
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

HEAVY_IO_LOCK_KEY = "lock:heavy-io"
RESOURCE_NETWORK_TOKEN_KEY = "lock:resource-budget:network"
RESOURCE_DISK_TOKEN_KEY = "lock:resource-budget:disk"
DEFAULT_LEASE_SECONDS = 300
DEFAULT_RENEW_SECONDS = 60
DEFAULT_POLL_SECONDS = 10.0
DEFAULT_REQUEUE_SECONDS = 10.0
LOCAL_LOCK_FILENAME = "heavy-io.lock"
RESOURCE_WORK_CHANNEL_PREFIX = "resource:work:"

# Set by ResourceAwareWorker immediately before fork.  The child inherits both
# this marker and the already-locked file descriptor.  Re-opening the file and
# flocking a second open-file description would self-conflict on Linux.
_worker_flock_inherited = False
_worker_inherited_profile: str | None = None

_lease_redis_client = None
_lease_redis_pid: int | None = None
_lease_redis_guard = threading.Lock()

_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""

_RESERVE_SCRIPT = """
if redis.call("exists", KEYS[1]) == 1 then
  return 0
end
local total = 0
for index = 2, #KEYS do
  local raw = redis.call("get", KEYS[index])
  if raw then
    local ok, payload = pcall(cjson.decode, raw)
    if not ok or payload["reserved_bytes"] == nil then
      return -2
    end
    total = total + tonumber(payload["reserved_bytes"])
  end
end
local requested = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
if total + requested > capacity then
  return -1
end
local result = redis.call("set", KEYS[1], ARGV[1], "NX", "EX", ARGV[4])
if result then
  return 1
end
return 0
"""

F = TypeVar("F", bound=Callable[..., Any])
ResourceStateCallback = Callable[[str, str, str | None], Any]
_resource_state_callback: ResourceStateCallback | None = None


def register_resource_state_callback(callback: ResourceStateCallback | None) -> None:
    """Register an optional TaskRun bridge without coupling this module to ORM."""

    global _resource_state_callback
    _resource_state_callback = callback


def _update_current_job_resource_meta(
    owner: str,
    state: str,
    reason: str | None,
    workload: str | None,
) -> None:
    try:
        from rq import get_current_job

        job = get_current_job()
        if job is None:
            return
        payload = {
            "owner": str(owner),
            "state": state,
            "reason": reason,
            "workload": workload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata = getattr(job, "meta", None)
        if not isinstance(metadata, dict):
            metadata = {}
            job.meta = metadata
        previous = metadata.get("resource_governance")
        if isinstance(previous, dict) and all(
            previous.get(key) == payload.get(key)
            for key in ("owner", "state", "reason", "workload")
        ):
            return
        metadata["resource_governance"] = payload
        metadata["resource_state"] = state
        job.save_meta()
    except Exception:
        logger.debug("Unable to update RQ resource-state metadata", exc_info=True)


async def set_resource_state(
    owner: str,
    state: str,
    reason: str | None = None,
    *,
    workload: str | None = None,
) -> None:
    """Expose running/waiting/yielded state through RQ and an optional callback."""

    if state not in {"running", "waiting", "yielded"}:
        raise ValueError(f"unsupported resource state: {state}")
    await asyncio.to_thread(
        _update_current_job_resource_meta,
        owner,
        state,
        reason,
        workload,
    )
    callback = _resource_state_callback
    if callback is None:
        return
    try:
        result = callback(str(owner), state, reason)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning("Resource-state callback failed owner=%s state=%s", owner, state, exc_info=True)


class HeavyIOUnavailable(RuntimeError):
    """The workload must remain queued because admission is temporarily closed."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def _local_lock_path() -> Path:
    configured = os.environ.get("HEAVY_IO_LOCK_PATH")
    if configured:
        return Path(configured)
    download_root = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads"))
    return download_root / ".locks" / LOCAL_LOCK_FILENAME


def workload_conflict_group(workload: str) -> str | None:
    """Map a precise resource profile to its exclusive conflict group."""

    from app.services.resource_pressure import workload_profile_name

    profile = workload_profile_name(workload)
    if profile == "download_network":
        # Download concurrency is budgeted separately; only a source archive is
        # exclusive, and that identity is resolved inside the download job.
        return None
    if profile in {"image_derive", "video_derive"}:
        return "media"
    if profile == "maintenance":
        return "maintenance"
    if profile == "light":
        return None
    return profile.replace("_", "-")


def profile_lease_key(workload: str) -> str | None:
    group = workload_conflict_group(workload)
    if group is None:
        return None
    if group == "maintenance":
        return HEAVY_IO_LOCK_KEY
    return f"lock:resource-profile:{group}"


def resource_budget_token_key(workload: str) -> str | None:
    """Return the v1 aggregate reservation token for a workload profile."""

    from app.services.resource_pressure import workload_profile_name

    profile = workload_profile_name(workload)
    if profile == "light":
        return None
    if profile == "maintenance":
        return HEAVY_IO_LOCK_KEY
    if profile == "download_network":
        return RESOURCE_NETWORK_TOKEN_KEY
    # First rollout: one bounded disk slice at a time.  This is deliberately
    # stricter than future weighted multi-token admission, but unlike the old
    # global lock it still permits network and foreground/light work to coexist.
    return RESOURCE_DISK_TOKEN_KEY


def resource_lease_keys(workload: str) -> list[str]:
    """Fixed-order aggregate token followed by an optional conflict lease."""

    token_key = resource_budget_token_key(workload)
    profile_key = profile_lease_key(workload)
    return list(dict.fromkeys(key for key in (token_key, profile_key) if key))


def resource_reservation_details(
    snapshot: dict[str, Any],
    workload: str,
) -> tuple[int, int]:
    """Return requested bytes and current capacity above the hard RAM floor."""

    from app.services.resource_pressure import (
        RESOURCE_PROFILES,
        current_memory_available_bytes,
        workload_profile_name,
    )

    budget = snapshot.get("budget") or {}
    profile_name = workload_profile_name(workload)
    profile = (budget.get("profiles") or {}).get(profile_name) or {}
    memory = snapshot.get("memory") or {}
    hard_limits = (snapshot.get("controller") or {}).get("hard_limits") or {}
    requested = int(RESOURCE_PROFILES[profile_name].memory_reservation_bytes)
    try:
        requested = max(
            0,
            int(profile.get("memory_reservation_bytes", requested)),
        )
        available = int(memory.get("available_bytes"))
        hard_floor = int(hard_limits.get("memory_available_bytes"))
    except (TypeError, ValueError):
        return requested, 0
    try:
        # The shared snapshot is intentionally written only on changes or a
        # 30-second heartbeat.  Re-read one cheap meminfo field at grant time so
        # aggregate network+disk reservations cannot consume a stale headroom
        # estimate while preserving the low Redis write budget.
        available = min(available, int(current_memory_available_bytes()))
    except (OSError, ValueError, TypeError):
        return requested, 0
    return requested, max(0, available - hard_floor)


def local_lock_for_workload(
    workload: str,
    *,
    source_identity: str | None = None,
) -> "LocalResourceLocks | None":
    from app.services.resource_pressure import workload_profile_name

    profile = workload_profile_name(workload)
    group = workload_conflict_group(workload)
    if group == "maintenance":
        return LocalResourceLocks([LocalHeavyIOLock()])
    if profile == "light":
        return None

    # Every ordinary slice takes a shared maintenance barrier first.  Only
    # maintenance takes the matching exclusive flock, so profiles may coexist
    # but VACUUM/backup waits for all of them to finish and blocks new slices.
    locks = [LocalHeavyIOLock(shared=True)]
    if group is not None:
        locks.append(
            LocalHeavyIOLock(_local_lock_path().with_name(f"profile-{group}.lock"))
        )
    if source_identity:
        digest = hashlib.sha256(
            source_identity.encode("utf-8", "surrogatepass")
        ).hexdigest()[:24]
        locks.append(
            LocalHeavyIOLock(_local_lock_path().with_name(f"source-{digest}.lock"))
        )
    return LocalResourceLocks(locks)


def local_source_lock(source_identity: str) -> "LocalResourceLocks":
    digest = hashlib.sha256(
        source_identity.encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return LocalResourceLocks(
        [LocalHeavyIOLock(_local_lock_path().with_name(f"source-{digest}.lock"))]
    )


class LocalHeavyIOLock:
    """Process-lifetime heavy-I/O mutex on the shared downloads volume."""

    def __init__(self, path: str | Path | None = None, *, shared: bool = False) -> None:
        self.path = Path(path) if path is not None else _local_lock_path()
        self.shared = bool(shared)
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o660)
        try:
            mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise

        self._fd = fd
        # Best-effort diagnostics only.  Never weaken the lock because the NAS
        # rejected metadata writes after flock itself succeeded.
        if not self.shared:
            try:
                payload = f"pid={os.getpid()} acquired_at={time.time():.6f}\n".encode()
                os.ftruncate(fd, 0)
                os.write(fd, payload)
            except OSError:
                logger.debug("Unable to write heavy-I/O lock metadata", exc_info=True)
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "LocalHeavyIOLock":
        if not self.try_acquire():
            raise HeavyIOUnavailable("heavy_io_busy")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class LocalResourceLocks:
    """Fixed-order maintenance barrier plus one profile/source mutex."""

    def __init__(self, locks: list[LocalHeavyIOLock]) -> None:
        self.locks = locks

    @property
    def path(self) -> Path:
        return self.locks[-1].path

    def try_acquire(self) -> bool:
        acquired: list[LocalHeavyIOLock] = []
        try:
            for lock in self.locks:
                if not lock.try_acquire():
                    for held in reversed(acquired):
                        held.release()
                    return False
                acquired.append(lock)
            return True
        except BaseException:
            for held in reversed(acquired):
                held.release()
            raise

    def release(self) -> None:
        for lock in reversed(self.locks):
            lock.release()


def mark_worker_flock_inherited(value: bool | str) -> None:
    """Mark the worker-parent lock as inherited by the next forked workhorse."""

    global _worker_flock_inherited, _worker_inherited_profile
    _worker_flock_inherited = bool(value)
    if isinstance(value, str):
        _worker_inherited_profile = value
    elif value:
        _worker_inherited_profile = "legacy-global"
    else:
        _worker_inherited_profile = None


def worker_flock_is_inherited() -> bool:
    return _worker_flock_inherited


def worker_inherited_profile() -> str | None:
    return _worker_inherited_profile


def _get_lease_redis():
    """Return a small Redis client with bounded socket operations.

    The main shared RQ pool intentionally cannot use a short socket timeout
    because workers perform blocking dequeues.  Lease heartbeats must be
    bounded independently so a black-holed connection cannot strand cleanup.
    """

    global _lease_redis_client, _lease_redis_pid
    process_id = os.getpid()
    with _lease_redis_guard:
        if _lease_redis_client is None or _lease_redis_pid != process_id:
            import redis

            from app.config import settings

            _lease_redis_client = redis.from_url(
                settings.redis_url,
                max_connections=4,
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=False,
                health_check_interval=30,
                decode_responses=False,
            )
            _lease_redis_pid = process_id
    return _lease_redis_client


def source_lock_key(source_identity: str) -> str:
    """Return a bounded Redis key without exposing a source URL."""

    digest = hashlib.sha256(source_identity.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return f"lock:heavy-io:source:{digest}"


async def _pressure_snapshot() -> dict[str, Any]:
    """Read the pressure service through its stable async contract.

    The import fallback is intentionally fail-open only for rolling upgrades
    where the pressure module is not present yet.  Once installed, its own
    ``paused`` state includes fail-closed handling for unreadable core metrics.
    """

    try:
        from app.services.resource_pressure import get_resource_pressure_snapshot
    except ImportError:
        return {"status": "normal", "reasons": [], "compatibility_fallback": True}

    try:
        value = get_resource_pressure_snapshot()
        if inspect.isawaitable(value):
            value = await value
        return value if isinstance(value, dict) else {"status": "paused", "reasons": ["pressure_snapshot_invalid"]}
    except Exception:
        logger.exception("Resource pressure snapshot failed; pausing heavy work")
        return {"status": "paused", "reasons": ["pressure_snapshot_error"]}


async def resource_pressure_paused() -> tuple[bool, dict[str, Any]]:
    snapshot = await _pressure_snapshot()
    return snapshot.get("status") == "paused", snapshot


async def resource_profile_permitted(
    workload: str,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    from app.services.resource_pressure import resource_profile_permit

    snapshot = await _pressure_snapshot()
    permitted, profile = resource_profile_permit(snapshot, workload)
    return permitted, snapshot, profile


def _adaptive_poll_seconds(attempt: int, initial: float) -> float:
    base = min(30.0, max(2.0, initial) * (2 ** max(0, attempt)))
    return max(2.0, min(30.0, base * random.uniform(0.85, 1.15)))


async def _wait_for_resource_event(workload: str, seconds: float) -> None:
    """Wait for a mode/budget event, using timeout when pub/sub is unavailable."""

    pubsub = None
    try:
        from app.services.resource_pressure import RESOURCE_CONTROL_CHANNEL

        pubsub = _get_lease_redis().pubsub(ignore_subscribe_messages=False)
        pubsub.subscribe(
            RESOURCE_CONTROL_CHANNEL,
            f"{RESOURCE_WORK_CHANNEL_PREFIX}{workload_profile_channel(workload)}",
        )
        # Redis sends one acknowledgement per subscribed channel.  Leaving the
        # second ACK queued makes the apparent event wait return immediately
        # and turns a critical worker into a subscribe/close busy loop.
        subscribed = 0
        setup_deadline = time.monotonic() + 1.0
        while subscribed < 2 and time.monotonic() < setup_deadline:
            message = await asyncio.to_thread(pubsub.get_message, timeout=0.25)
            if not message:
                continue
            message_type = str(message.get("type") or "")
            if message_type in {"subscribe", "psubscribe"}:
                subscribed += 1
            elif message_type in {"message", "pmessage"}:
                return
        await asyncio.to_thread(pubsub.get_message, timeout=max(0.05, seconds))
        return
    except Exception:
        logger.debug("Resource event wait unavailable", exc_info=True)
    finally:
        if pubsub is not None:
            try:
                await asyncio.to_thread(pubsub.close)
            except Exception:
                pass
    await asyncio.sleep(max(0.05, seconds))


def workload_profile_channel(workload: str) -> str:
    from app.services.resource_pressure import workload_profile_name

    return workload_profile_name(workload)


async def wait_for_resource_capacity(
    *,
    workload: str,
    owner: str,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> dict[str, Any]:
    """Wait until the host pressure controller permits heavy work."""

    logged_wait = False
    attempt = 0
    while True:
        permitted, snapshot, profile = await resource_profile_permitted(workload)
        if permitted:
            if logged_wait:
                logger.info("Resource pressure recovered; resuming %s owner=%s", workload, owner)
            await set_resource_state(owner, "running", None, workload=workload)
            return snapshot
        if not logged_wait:
            logger.warning(
                "Resource budget waiting %s owner=%s profile_reason=%s reasons=%s",
                workload,
                owner,
                profile.get("reason"),
                snapshot.get("reasons") or [],
            )
            logged_wait = True
        reason = str(profile.get("reason") or "resource_pressure")
        await set_resource_state(owner, "waiting", reason, workload=workload)
        await _wait_for_resource_event(
            workload,
            _adaptive_poll_seconds(attempt, poll_seconds),
        )
        attempt += 1


class RenewableRedisLease:
    """A token-owned Redis auxiliary lease with a thread-based heartbeat."""

    def __init__(
        self,
        keys: list[str],
        *,
        workload: str,
        owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        renew_seconds: int = DEFAULT_RENEW_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        redis_client=None,
        reservation_key: str | None = None,
        reservation_bytes: int = 0,
        reservation_capacity_bytes: int = 0,
    ) -> None:
        if not keys:
            raise ValueError("at least one Redis lease key is required")
        self.keys = list(dict.fromkeys(keys))
        self.workload = workload
        self.owner = owner
        self.lease_seconds = max(30, int(lease_seconds))
        self.renew_seconds = max(5, min(int(renew_seconds), self.lease_seconds // 2))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.redis = redis_client or _get_lease_redis()
        self.token = uuid.uuid4().hex
        self.reservation_key = reservation_key if reservation_key in self.keys else None
        if self.reservation_key is not None:
            self.keys.remove(self.reservation_key)
            self.keys.insert(0, self.reservation_key)
        self.reservation_bytes = max(0, int(reservation_bytes))
        self.reservation_capacity_bytes = max(0, int(reservation_capacity_bytes))
        self.lease_value = json.dumps(
            {
                "token": self.token,
                "owner": str(owner),
                "workload": str(workload),
                "profile": workload_profile_channel(workload),
                "reserved_bytes": self.reservation_bytes,
                "capacity_bytes": self.reservation_capacity_bytes,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )
        self.denial_reason: str | None = None
        self.acquired = False
        self.lease_lost = threading.Event()
        self._stop_renewal = threading.Event()
        self._renew_thread: threading.Thread | None = None

    async def _release_keys(self, keys: list[str]) -> None:
        for key in reversed(keys):
            try:
                await asyncio.to_thread(
                    self.redis.eval,
                    _RELEASE_SCRIPT,
                    1,
                    key,
                    self.lease_value,
                )
            except Exception:
                logger.warning("Failed to release Redis lease %s", key, exc_info=True)

    async def try_acquire(self) -> bool:
        acquired: list[str] = []
        self.denial_reason = None
        try:
            for key in self.keys:
                if key == self.reservation_key:
                    reserve_keys = list(
                        dict.fromkeys(
                            [
                                key,
                                RESOURCE_NETWORK_TOKEN_KEY,
                                RESOURCE_DISK_TOKEN_KEY,
                            ]
                        )
                    )
                    result = await asyncio.to_thread(
                        self.redis.eval,
                        _RESERVE_SCRIPT,
                        len(reserve_keys),
                        *reserve_keys,
                        self.lease_value,
                        self.reservation_bytes,
                        self.reservation_capacity_bytes,
                        self.lease_seconds,
                    )
                    ok = int(result or 0) == 1
                    if not ok:
                        self.denial_reason = (
                            "resource_reservation_capacity"
                            if int(result or 0) in {-1, -2}
                            else "resource_reservation_busy"
                        )
                else:
                    ok = await asyncio.to_thread(
                        self.redis.set,
                        key,
                        self.lease_value,
                        nx=True,
                        ex=self.lease_seconds,
                    )
                if not ok:
                    await self._release_keys(acquired)
                    return False
                acquired.append(key)
        except Exception:
            await self._release_keys(acquired)
            logger.warning("Unable to acquire Redis heavy-I/O lease", exc_info=True)
            return False
        self.acquired = True
        return True

    def _renew_loop(self) -> None:
        # This must not run on the workload's event loop: download.py contains
        # intentionally blocking subprocess polling, and an asyncio task would
        # miss its heartbeat until the 300-second lease had already expired.
        while not self._stop_renewal.wait(self.renew_seconds):
            if not self.acquired:
                return
            try:
                renewed = []
                for key in self.keys:
                    result = self.redis.eval(
                        _RENEW_SCRIPT,
                        1,
                        key,
                        self.lease_value,
                        self.lease_seconds,
                    )
                    renewed.append(bool(result))
                if not all(renewed):
                    self.lease_lost.set()
                    logger.critical(
                        "Heavy-I/O lease ownership was lost workload=%s owner=%s keys=%s",
                        self.workload,
                        self.owner,
                        self.keys,
                    )
                    return
            except Exception:
                # The existing TTL remains a safety window.  Keep retrying so a
                # brief Redis outage does not create a second active writer.
                logger.warning(
                    "Heavy-I/O lease renewal failed workload=%s owner=%s",
                    self.workload,
                    self.owner,
                    exc_info=True,
                )

    async def acquire(self) -> "RenewableRedisLease":
        if not await self.try_acquire():
            raise HeavyIOUnavailable("redis_lease_busy")
        self.start_renewal()
        logger.info(
            "Acquired heavy-I/O lease workload=%s owner=%s",
            self.workload,
            self.owner,
        )
        return self

    def start_renewal(self) -> None:
        if self.acquired and self._renew_thread is None:
            self._stop_renewal.clear()
            self._renew_thread = threading.Thread(
                target=self._renew_loop,
                name=f"heavy-io-renew-{self.owner}"[:64],
                daemon=True,
            )
            self._renew_thread.start()

    async def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        self._stop_renewal.set()
        if self._renew_thread:
            # Socket operations are bounded for the production lease client.
            # A custom test/client may still misbehave, so never let cleanup
            # wait indefinitely for an observability-only heartbeat.
            await asyncio.to_thread(self._renew_thread.join, 3.0)
            if self._renew_thread.is_alive():
                logger.error(
                    "Heavy-I/O lease heartbeat did not stop promptly workload=%s owner=%s",
                    self.workload,
                    self.owner,
                )
            self._renew_thread = None
        await self._release_keys(self.keys)
        logger.info("Released heavy-I/O lease workload=%s owner=%s", self.workload, self.owner)

    async def __aenter__(self) -> "RenewableRedisLease":
        return await self.acquire()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


def collect_active_resource_leases(redis_client=None) -> dict[str, Any]:
    """Return deduplicated token reservations for health and baseline gating."""

    client = redis_client or _get_lease_redis()
    keys = list(
        dict.fromkeys(
            [
                HEAVY_IO_LOCK_KEY,
                RESOURCE_NETWORK_TOKEN_KEY,
                RESOURCE_DISK_TOKEN_KEY,
                *(profile_lease_key(profile) for profile in (
                    "import_db",
                    "image_derive",
                    "video_derive",
                    "search_index",
                    "git_projection",
                )),
            ]
        )
    )
    try:
        pipeline = client.pipeline(transaction=False)
        for key in keys:
            pipeline.get(key)
            pipeline.ttl(key)
        values = pipeline.execute()
    except Exception as exc:
        return {
            "mode": "single_disk_token_v1",
            "active": [],
            "active_count": 0,
            "reserved_bytes": None,
            "error": type(exc).__name__,
        }

    by_token: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    for index, key in enumerate(keys):
        raw = values[index * 2]
        raw_ttl = values[index * 2 + 1]
        try:
            ttl = int(raw_ttl) if raw_ttl is not None else -1
        except (TypeError, ValueError):
            ttl = -1
        if raw is None:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(str(raw))
            if not isinstance(payload, dict):
                raise ValueError("lease payload is not an object")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {
                "token": str(raw),
                "owner": None,
                "profile": None,
                "workload": None,
                "reserved_bytes": 0,
                "capacity_bytes": None,
                "legacy": True,
            }
        token = str(payload.get("token") or raw)
        lease = by_token.setdefault(
            token,
            {
                **payload,
                "token": token,
                "keys": [],
                "ttl_seconds": ttl,
            },
        )
        lease["keys"].append(key)
        positive_ttls = [value for value in (lease.get("ttl_seconds"), ttl) if value >= 0]
        lease["ttl_seconds"] = min(positive_ttls) if positive_ttls else -1

    active = []
    for lease in by_token.values():
        raw_ttl = lease.get("ttl_seconds")
        try:
            ttl = int(raw_ttl) if raw_ttl is not None else -1
        except (TypeError, ValueError):
            ttl = -1
        lease["expires_at"] = (
            datetime.fromtimestamp(now.timestamp() + ttl, tz=timezone.utc).isoformat()
            if ttl >= 0
            else None
        )
        active.append(lease)
    active.sort(key=lambda value: (str(value.get("profile")), str(value.get("owner"))))
    return {
        "mode": "single_disk_token_v1",
        "active": active,
        "active_count": len(active),
        "reserved_bytes": sum(int(value.get("reserved_bytes") or 0) for value in active),
        "network_active": any(RESOURCE_NETWORK_TOKEN_KEY in value["keys"] for value in active),
        "disk_active": any(RESOURCE_DISK_TOKEN_KEY in value["keys"] for value in active),
        "maintenance_active": any(HEAVY_IO_LOCK_KEY in value["keys"] for value in active),
    }


@asynccontextmanager
async def heavy_io_slot(
    workload: str,
    owner: str,
    *,
    source_identity: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    renew_seconds: int = DEFAULT_RENEW_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    redis_client=None,
):
    from app.services.resource_pressure import workload_profile_name

    profile_name = workload_profile_name(workload)
    conflict_group = workload_conflict_group(workload)
    inherited_profile = worker_inherited_profile()
    inherited_flock = worker_flock_is_inherited() and inherited_profile in {
        "legacy-global",
        profile_name,
    }
    needs_child_budget_token = (
        inherited_flock
        and inherited_profile == "download_network"
        and profile_name == "download_network"
    )
    snapshot: dict[str, Any] = {}
    if not inherited_flock or needs_child_budget_token:
        permitted, snapshot, profile = await resource_profile_permitted(workload)
        if not permitted:
            reason = str(profile.get("reason") or "resource_pressure")
            await set_resource_state(owner, "waiting", reason, workload=workload)
            raise HeavyIOUnavailable(
                "resource_pressure",
                {
                    "reasons": snapshot.get("reasons") or [],
                    "controller_mode": snapshot.get("controller_mode"),
                    "profile": profile,
                },
            )
        if profile_name != "maintenance" and not inherited_flock:
            try:
                maintenance_pending = await asyncio.to_thread(
                    (redis_client or _get_lease_redis()).exists,
                    HEAVY_IO_LOCK_KEY,
                )
            except Exception as exc:
                raise HeavyIOUnavailable(
                    "redis_lease_unavailable",
                    {"error_type": type(exc).__name__},
                ) from exc
            if maintenance_pending:
                await set_resource_state(
                    owner,
                    "waiting",
                    "maintenance_pending",
                    workload=workload,
                )
                raise HeavyIOUnavailable("maintenance_pending")

    local_lock: LocalResourceLocks | None = None
    keys: list[str] = (
        resource_lease_keys(workload)
        if not inherited_flock or needs_child_budget_token
        else []
    )
    if source_identity:
        keys.append(source_lock_key(source_identity))
    lease: RenewableRedisLease | None = None
    try:
        redis_acquired = True
        if keys:
            reservation_bytes, reservation_capacity = resource_reservation_details(
                snapshot,
                workload,
            )
            if reservation_bytes > reservation_capacity:
                await set_resource_state(
                    owner,
                    "waiting",
                    "profile_memory_reserve_live",
                    workload=workload,
                )
                raise HeavyIOUnavailable("profile_memory_reserve_live")
            budget_token = resource_budget_token_key(workload)
            lease = RenewableRedisLease(
                keys,
                workload=workload,
                owner=owner,
                lease_seconds=lease_seconds,
                renew_seconds=renew_seconds,
                poll_seconds=poll_seconds,
                redis_client=redis_client,
                reservation_key=(
                    budget_token
                    if (not inherited_flock or needs_child_budget_token)
                    and budget_token in {
                        RESOURCE_NETWORK_TOKEN_KEY,
                        RESOURCE_DISK_TOKEN_KEY,
                    }
                    else None
                ),
                reservation_bytes=reservation_bytes,
                reservation_capacity_bytes=reservation_capacity,
            )
            redis_acquired = await lease.try_acquire()
            if redis_acquired:
                lease.start_renewal()
            elif not inherited_flock or needs_child_budget_token:
                denial_reason = lease.denial_reason or "redis_lease_busy"
                await set_resource_state(owner, "waiting", denial_reason, workload=workload)
                raise HeavyIOUnavailable(denial_reason)
            else:
                logger.warning(
                    "Continuing profile job with inherited authoritative lock but "
                    "without Redis lease workload=%s owner=%s profile=%s",
                    workload,
                    owner,
                    conflict_group,
                )
        needs_inherited_source_lock = (
            inherited_flock
            and profile_name == "download_network"
            and source_identity is not None
        )
        if not inherited_flock or needs_inherited_source_lock:
            local_lock = (
                local_source_lock(source_identity)
                if needs_inherited_source_lock and source_identity is not None
                else local_lock_for_workload(
                    workload,
                    source_identity=source_identity,
                )
            )
            if local_lock is not None:
                try:
                    local_acquired = local_lock.try_acquire()
                except OSError as exc:
                    logger.error("Unable to acquire resource-profile lock", exc_info=True)
                    raise HeavyIOUnavailable(
                        "local_lock_unavailable",
                        {"error_type": type(exc).__name__},
                    ) from exc
                if not local_acquired:
                    await set_resource_state(
                        owner,
                        "waiting",
                        "profile_busy",
                        workload=workload,
                    )
                    raise HeavyIOUnavailable("profile_busy")
        logger.info(
            "Entered heavy-I/O slot workload=%s owner=%s inherited_flock=%s redis_lease=%s",
            workload,
            owner,
            inherited_flock,
            redis_acquired,
        )
        await set_resource_state(owner, "running", None, workload=workload)
        yield lease
    finally:
        if lease is not None:
            await lease.release()
        if local_lock is not None:
            local_lock.release()


@asynccontextmanager
async def adaptive_resource_slice(
    workload: str,
    owner: str,
    *,
    max_work_units: int | None = None,
    max_slice_seconds: float | None = None,
    source_identity: str | None = None,
    wait_for_capacity: bool = True,
    cooldown_result: dict[str, float] | None = None,
):
    """Acquire one self-renewing profile slice and cool down lock-free.

    Coordinator RQ jobs are deliberately classified as ``light`` in the
    parent worker.  This child-side context therefore owns the renewable Redis
    reservation as well as the POSIX profile lock for exactly one bounded
    action.  Resource waits happen before either is held; enforce-mode duty
    cycle sleep happens after both are released.
    """

    from app.services.resource_pressure import (
        current_profile_slice_limits,
        profile_slice_cooldown_seconds,
        sleep_for_profile_slice_cooldown,
    )

    attempt = 0
    stack: AsyncExitStack | None = None
    snapshot: dict[str, Any] = {}
    limits = None
    while stack is None:
        limits, snapshot = await current_profile_slice_limits(
            workload,
            max_work_units=max_work_units,
            max_slice_seconds=max_slice_seconds,
        )
        if not limits.allowed:
            if not wait_for_capacity:
                yield None
                return
            await wait_for_resource_capacity(workload=workload, owner=owner)
            continue
        candidate = AsyncExitStack()
        try:
            await candidate.enter_async_context(
                heavy_io_slot(
                    workload,
                    owner,
                    source_identity=source_identity,
                )
            )
        except HeavyIOUnavailable as error:
            await candidate.aclose()
            await set_resource_state(
                owner,
                "waiting",
                error.reason,
                workload=workload,
            )
            if not wait_for_capacity:
                yield None
                return
            await _wait_for_resource_event(
                workload,
                _adaptive_poll_seconds(attempt, DEFAULT_POLL_SECONDS),
            )
            attempt += 1
        else:
            stack = candidate

    assert stack is not None and limits is not None
    started = time.monotonic()
    try:
        yield limits
    finally:
        elapsed = time.monotonic() - started
        await stack.aclose()
        await set_resource_state(
            owner,
            "yielded",
            "slice_complete",
            workload=workload,
        )
        if cooldown_result is not None:
            cooldown_result["seconds"] = profile_slice_cooldown_seconds(
                snapshot,
                elapsed_seconds=elapsed,
                # At 10% a 20-second slice needs 180 seconds idle.  Capping at
                # 30 would silently turn the promised 10% budget into 40%.
                max_seconds=300.0,
                workload=workload,
            )
        else:
            await sleep_for_profile_slice_cooldown(
                snapshot,
                elapsed_seconds=elapsed,
                max_seconds=300.0,
                workload=workload,
            )


@asynccontextmanager
async def try_heavy_io_slot(
    workload: str,
    owner: str,
    *,
    redis_client=None,
):
    """Try pressure, local flock, and Redis once without waiting."""

    from app.services.resource_pressure import workload_profile_name

    profile_name = workload_profile_name(workload)
    conflict_group = workload_conflict_group(workload)
    inherited_profile = worker_inherited_profile()
    inherited_flock = worker_flock_is_inherited() and inherited_profile in {
        "legacy-global",
        profile_name,
    }
    snapshot: dict[str, Any] = {}
    if not inherited_flock:
        permitted, snapshot, profile = await resource_profile_permitted(workload)
        if not permitted:
            await set_resource_state(
                owner,
                "waiting",
                str(profile.get("reason") or "resource_pressure"),
                workload=workload,
            )
            yield None
            return
        if profile_name != "maintenance":
            try:
                maintenance_pending = await asyncio.to_thread(
                    (redis_client or _get_lease_redis()).exists,
                    HEAVY_IO_LOCK_KEY,
                )
            except Exception:
                yield None
                return
            if maintenance_pending:
                await set_resource_state(
                    owner,
                    "waiting",
                    "maintenance_pending",
                    workload=workload,
                )
                yield None
                return

    local_lock: LocalResourceLocks | None = None
    lease: RenewableRedisLease | None = None
    try:
        redis_acquired = True
        lease_keys = resource_lease_keys(workload) if not inherited_flock else []
        if lease_keys:
            reservation_bytes, reservation_capacity = resource_reservation_details(
                snapshot,
                workload,
            )
            if reservation_bytes > reservation_capacity:
                await set_resource_state(
                    owner,
                    "waiting",
                    "profile_memory_reserve_live",
                    workload=workload,
                )
                yield None
                return
            budget_token = resource_budget_token_key(workload)
            lease = RenewableRedisLease(
                lease_keys,
                workload=workload,
                owner=owner,
                redis_client=redis_client,
                reservation_key=(
                    budget_token
                    if budget_token in {
                        RESOURCE_NETWORK_TOKEN_KEY,
                        RESOURCE_DISK_TOKEN_KEY,
                    }
                    else None
                ),
                reservation_bytes=reservation_bytes,
                reservation_capacity_bytes=reservation_capacity,
            )
            redis_acquired = await lease.try_acquire()
            if redis_acquired:
                lease.start_renewal()
            elif not inherited_flock:
                denial_reason = lease.denial_reason or "redis_lease_busy"
                await set_resource_state(owner, "waiting", denial_reason, workload=workload)
                yield None
                return
            else:
                logger.warning(
                    "Continuing profile slice with inherited lock but without Redis "
                    "lease workload=%s owner=%s profile=%s",
                    workload,
                    owner,
                    conflict_group,
                )
        if not inherited_flock:
            local_lock = local_lock_for_workload(workload)
            if local_lock is not None:
                try:
                    local_acquired = local_lock.try_acquire()
                except OSError:
                    logger.error("Unable to acquire resource-profile lock", exc_info=True)
                    yield None
                    return
                if not local_acquired:
                    await set_resource_state(
                        owner,
                        "waiting",
                        "profile_busy",
                        workload=workload,
                    )
                    yield None
                    return
        await set_resource_state(owner, "running", None, workload=workload)
        # ``None`` is the public denial signal used by non-blocking callers.
        # When the RQ parent already owns the authoritative POSIX lock there is
        # intentionally no child Redis lease, but admission did succeed.
        yield lease if lease is not None else True
    finally:
        if lease is not None:
            await lease.release()
        if local_lock is not None:
            local_lock.release()


def _rq_owner(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    from uuid import UUID

    for value in (
        kwargs.get("job_id"),
        kwargs.get("task_id"),
        kwargs.get("download_job_id"),
        kwargs.get("import_job_id"),
        *args,
    ):
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    try:
        from rq import get_current_job

        job = get_current_job()
        if job:
            return str(job.id)
    except Exception:
        pass
    value = kwargs.get("job_id") or (args[0] if args else "unknown")
    return str(value)


def _defer_current_rq_job(unavailable: HeavyIOUnavailable):
    """Defer only fallback RQ execution that bypassed the dedicated worker gate."""

    try:
        from rq import Retry, get_current_job

        job = get_current_job()
        if job is not None:
            _update_current_job_resource_meta(
                str(job.id),
                "waiting",
                unavailable.reason,
                None,
            )
            logger.info(
                "Deferring heavy-I/O job before workload start job=%s reason=%s",
                job.id,
                unavailable.reason,
            )
            # Dedicated heavy workers never reach this path: they hold flock
            # before dequeue and inherited slots treat Redis as best-effort.
            # Keep a delayed fallback for direct/SimpleWorker invocations.
            return Retry(max=1_000_000, interval=int(DEFAULT_REQUEUE_SECONDS))
    except ImportError:
        pass
    raise unavailable


def heavy_io_sync_job(workload: str) -> Callable[[F], F]:
    """Decorate a synchronous RQ entry point with the renewable lease."""

    def decorate(func: F) -> F:
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            owner = _rq_owner(args, kwargs)

            async def guarded():
                try:
                    async with heavy_io_slot(workload, owner):
                        return await asyncio.to_thread(func, *args, **kwargs)
                except HeavyIOUnavailable as unavailable:
                    return _defer_current_rq_job(unavailable)

            return asyncio.run(guarded())

        return wrapped  # type: ignore[return-value]

    return decorate


async def run_heavy_io_operation(
    workload: str,
    owner: str,
    operation_factory: Callable[[], Awaitable[Any]],
) -> Any:
    """Run an async operation under the process lock and auxiliary lease."""

    try:
        async with heavy_io_slot(workload, owner):
            return await operation_factory()
    except HeavyIOUnavailable as unavailable:
        return _defer_current_rq_job(unavailable)


def heavy_io_async_job(
    workload: str,
    *,
    source_identity_resolver: Callable[..., str | None | Awaitable[str | None]] | None = None,
) -> Callable[[F], F]:
    """Decorate an async RQ function and optionally add a per-source lease."""

    def decorate(func: F) -> F:
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            owner = _rq_owner(args, kwargs)
            source_identity = None
            if source_identity_resolver:
                source_identity = source_identity_resolver(*args, **kwargs)
                if inspect.isawaitable(source_identity):
                    source_identity = await source_identity
            # Import ingestion is a control/transaction stage now; expensive
            # curation, media, search and Git work is drained by bounded outbox
            # jobs.  Holding import_db across the whole legacy runner would
            # recreate global idle time and block its own 25-item projections.
            if str(workload).strip().lower() == "import":
                await wait_for_resource_capacity(
                    workload="import_db",
                    owner=owner,
                )
                return await func(*args, **kwargs)
            try:
                async with heavy_io_slot(
                    workload,
                    owner,
                    source_identity=source_identity,
                ):
                    return await func(*args, **kwargs)
            except HeavyIOUnavailable as unavailable:
                return _defer_current_rq_job(unavailable)

        return wrapped  # type: ignore[return-value]

    return decorate
