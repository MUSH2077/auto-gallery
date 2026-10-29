"""Download-only bridge for resource-deferred RQ retries."""

from __future__ import annotations

import json
from typing import Any, Iterable

from rq.job import Job
from rq.utils import current_timestamp

from app.services.download_concurrency import (
    MAX_DOWNLOAD_CONCURRENCY,
    execution_limit,
)



RESOURCE_RETRY_CONTEXT = "_auto_gallery_resource_wait"
RESOURCE_WAIT_MARKER = "auto_gallery:download_resource_wait"
RESOURCE_WAIT_REASONS = frozenset({
    "redis_lease_busy", "resource_reservation_busy", "resource_reservation_capacity",
    "resource_pressure", "profile_memory_reserve_live",
    "profile_busy", "maintenance_pending",
})


def is_download_queue(queue_name: str) -> bool:
    return queue_name == "downloads" or queue_name.startswith("downloads:")


def is_resource_reason(reason: str | None) -> bool:
    return str(reason or "") in RESOURCE_WAIT_REASONS


def attach_resource_retry_context(retry: Any, *, workload: str, reason: str, source_identity: str | None = None) -> Any:
    from app.services.resource_pressure import workload_profile_name
    if workload_profile_name(workload) != "download_network" or not is_resource_reason(reason):
        return retry
    setattr(retry, RESOURCE_RETRY_CONTEXT, {
        "workload": workload, "reason": reason, "source_identity": source_identity,
    })
    return retry


_REGISTER_SCRIPT = """
local key = KEYS[1]
if redis.call('HGET', key, 'status') ~= 'scheduled' then return 0 end
if tonumber(redis.call('HGET', key, 'number_of_retries') or '-1') ~= tonumber(ARGV[2]) then return 0 end
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not score or tonumber(score) ~= tonumber(ARGV[3]) then return 0 end
redis.call('HSET', key, ARGV[4], ARGV[5])
return 1
"""


def _marker_key(job: Job) -> str:
    return str(getattr(job, "key", f"rq:job:{job.id}"))


def register_after_retry_for_registry(connection, job: Job, retry: Any, *, registry, queue_name: str) -> bool:
    """Atomically bind a marker to the scheduled RQ retry just committed."""
    context = getattr(retry, RESOURCE_RETRY_CONTEXT, None)
    if not isinstance(context, dict) or not is_download_queue(queue_name):
        return False
    try:
        # RQ increments this Job object while committing the retry. Binding
        # to that attempt prevents a later business retry from reusing context.
        retry_count = int(getattr(job, "number_of_retries", 0) or 0)
        score = connection.zscore(registry.key, job.id)
        if score is None:
            return False
        marker = {
            "job_id": str(job.id), "retry_count": retry_count,
            "scheduled_score": float(score), "workload": "download_network",
            "reason": str(context.get("reason") or "resource_wait"),
            "source_identity": context.get("source_identity"),
        }
        raw = json.dumps(marker, separators=(",", ":"))
        changed = connection.eval(
            _REGISTER_SCRIPT, 2, _marker_key(job), registry.key, str(job.id),
            str(retry_count), str(float(score)), RESOURCE_WAIT_MARKER, raw,
        )
        if not int(changed or 0):
            return False
        from app.services.resource_pressure_snapshot import RESOURCE_CONTROL_CHANNEL
        connection.publish(RESOURCE_CONTROL_CHANNEL, json.dumps({
            "type": "download_resource_waiting", "job_id": str(job.id),
        }, separators=(",", ":")))
        return True
    except Exception:
        # The ordinary scheduled Retry remains the recovery path.
        return False


def clear_marker(connection, job_or_id: Job | str) -> None:
    key = _marker_key(job_or_id) if hasattr(job_or_id, "key") else f"rq:job:{job_or_id}"
    try:
        connection.hdel(key, RESOURCE_WAIT_MARKER)
    except Exception:
        pass


def _read_marker(connection, job_id: str) -> tuple[str | None, dict[str, Any] | None]:
    try:
        raw = connection.hget(f"rq:job:{job_id}", RESOURCE_WAIT_MARKER)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError, OSError):
        return None, None
    return raw, value if isinstance(value, dict) else None


def _lock_probe(source_identity: str | None) -> tuple[bool, int]:
    from app.services.heavy_io import parallel_download_locks
    occupied = 0
    available = False
    for slot in range(MAX_DOWNLOAD_CONCURRENCY):
        locks = parallel_download_locks(slot, source_identity)
        try:
            if locks.try_acquire():
                available = True
                locks.release()
            else:
                occupied += 1
        except OSError:
            occupied += 1
            try:
                locks.release()
            except Exception:
                pass
    return available, occupied


def resource_wait_ready(connection, marker: dict[str, Any]) -> bool:
    """Probe every authoritative guard without taking a Redis lease."""
    try:
        from app.services.heavy_io import (
            HEAVY_IO_LOCK_KEY,
            RESOURCE_NETWORK_TOKEN_KEY,
            LocalHeavyIOLock,
            _local_lock_path,
            DOWNLOAD_RESERVATION_KEYS,
            PARALLEL_DOWNLOAD_RESERVATION_BYTES,
            RESOURCE_RESERVATION_KEYS,
            resource_reservation_details,
            source_lock_key,
        )
        from app.services.resource_pressure import (
            get_resource_pressure_snapshot_sync,
            resource_profile_permit,
        )
        if connection.exists(HEAVY_IO_LOCK_KEY, RESOURCE_NETWORK_TOKEN_KEY):
            return False
        admission = LocalHeavyIOLock(_local_lock_path().with_name("network-admission.lock"))
        if not admission.try_acquire():
            return False
        admission.release()
        source = marker.get("source_identity")
        if source and connection.exists(source_lock_key(str(source))):
            # Redis is only a fast negative check; the POSIX probe below remains
            # authoritative when the auxiliary key was lost.
            return False
        snapshot = get_resource_pressure_snapshot_sync(redis_client=connection)
        allowed, _ = resource_profile_permit(snapshot, "download_network")
        if not allowed:
            return False
        requested, capacity = resource_reservation_details(snapshot, "download_network")
        limit = execution_limit()
        if limit > 1:
            requested = max(requested, PARALLEL_DOWNLOAD_RESERVATION_BYTES)
        total = 0
        network_active = 0
        for key in RESOURCE_RESERVATION_KEYS:
            raw = connection.get(key)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                total += int(payload["reserved_bytes"])
                network_active += key in DOWNLOAD_RESERVATION_KEYS
            except (TypeError, ValueError, KeyError):
                return False
        if total + requested > capacity or network_active >= limit:
            return False
        available, occupied = _lock_probe(str(source) if source else None)
        return available and occupied < limit
    except Exception:
        return False


_PROMOTE_SCRIPT = """
local key = KEYS[1]
local marker = redis.call('HGET', key, ARGV[1])
if not marker then return 0 end
if redis.call('HGET', key, 'status') ~= 'scheduled' then return 0 end
if tonumber(redis.call('HGET', key, 'number_of_retries') or '-1') ~= tonumber(ARGV[2]) then return 0 end
local score = redis.call('ZSCORE', KEYS[2], ARGV[3])
if not score or tonumber(score) ~= tonumber(ARGV[4]) then return 0 end
if marker ~= ARGV[5] then return 0 end
redis.call('ZADD', KEYS[2], ARGV[6], ARGV[3])
redis.call('HDEL', key, ARGV[1])
return 1
"""


def _marked_ids(connection, registry: Any, *, pages: int = 2, count: int = 50):
    """Scan a bounded moving window; cursor is kept on the registry instance."""
    cursor = int(getattr(registry, "_download_resource_wait_cursor", 0) or 0)
    for _ in range(pages):
        cursor, entries = connection.zscan(registry.key, cursor, count=count)
        for raw_id, score in entries:
            job_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            yield job_id, float(score)
        if cursor == 0:
            break
    registry._download_resource_wait_cursor = cursor


def promote_resource_waits(connection, registries: Iterable[Any], *, now: float | None = None) -> int:
    """Advance ready markers; RQ performs the normal promotion afterwards."""
    timestamp = float(current_timestamp() if now is None else now)
    promoted = 0
    promoted_sources: set[str] = set()
    for registry in registries:
        if not is_download_queue(str(getattr(registry, "name", ""))):
            continue
        for job_id, scheduled_score in _marked_ids(connection, registry):
            raw, marker = _read_marker(connection, job_id)
            if not marker or raw is None or scheduled_score <= timestamp:
                continue
            source = marker.get("source_identity")
            if source in promoted_sources or not resource_wait_ready(connection, marker):
                continue
            expected, retries = marker.get("scheduled_score"), marker.get("retry_count")
            if expected is None or retries is None:
                continue
            changed = connection.eval(
                _PROMOTE_SCRIPT, 2, f"rq:job:{job_id}", registry.key,
                RESOURCE_WAIT_MARKER, str(int(retries)), str(job_id),
                str(float(expected)), str(raw), str(timestamp),
            )
            promoted += int(changed or 0)
            if changed and source:
                # One archive can admit only one job. Avoid waking its whole
                # waiting cohort before the first workhorse obtains the lock.
                promoted_sources.add(source)
    return promoted
