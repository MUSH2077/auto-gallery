"""Shared status storage for background admin operations."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.services.redis_client import get_redis
from app.services.queue_admission import checked_enqueue, ensure_redis_enqueue_capacity

# Long adaptive rebuilds can legitimately spend several days yielding at the
# 10% budget floor.  Their single-flight/status records must outlive the RQ
# timeout or a second writer could be admitted while the first is still alive.
OPERATION_TTL_SECONDS = 8 * 24 * 60 * 60
logger = logging.getLogger(__name__)


_ACQUIRE_ATTEMPT_OPERATION_SCRIPT = """
local current = redis.call('get', KEYS[1])
local current_attempt = redis.call('get', KEYS[2])
if current then
  if current ~= ARGV[1] then
    return 0
  end
  if ARGV[4] ~= '1' then
    return 0
  end
end
if ARGV[5] == '1' then
  if current_attempt then
    return 0
  end
elseif current_attempt ~= ARGV[6] then
  return 0
end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""

_ACQUIRE_LEGACY_OPERATION_SCRIPT = """
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return 0
end
return redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) and 1 or 0
"""

_RELEASE_ATTEMPT_OPERATION_SCRIPT = """
local current = redis.call('get', KEYS[1])
local attempt = redis.call('get', KEYS[2])
if current == ARGV[1] and attempt == ARGV[2] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RELEASE_LEGACY_OPERATION_SCRIPT = """
if redis.call('exists', KEYS[2]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return 0
end
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_SET_LEGACY_OPERATION_STATUS_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 or redis.call('exists', KEYS[2]) == 1 then
  return 0
end
redis.call('setex', KEYS[3], ARGV[1], ARGV[2])
return 1
"""

_SET_ATTEMPT_OPERATION_STATUS_SCRIPT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then
  return 0
end
redis.call('setex', KEYS[2], ARGV[2], ARGV[3])
redis.call('setex', KEYS[3], ARGV[2], ARGV[1])
redis.call('expire', KEYS[1], ARGV[2])
return 1
"""

_GET_CURRENT_OPERATION_STATUS_SCRIPT = """
local attempt = redis.call('get', KEYS[1])
local cache_attempt = redis.call('get', KEYS[2])
if attempt then
  if not cache_attempt or cache_attempt ~= attempt then
    return false
  end
elseif cache_attempt then
  return false
end
return redis.call('get', KEYS[3])
"""


def operation_key(job_id: str) -> str:
    return f"admin_operation:{job_id}"


def operation_attempt_key(job_id: str) -> str:
    """Private current-attempt pointer for one logical admin TaskRun."""

    return f"admin_operation_owner:{job_id}"


def operation_cache_attempt_key(job_id: str) -> str:
    """Private attempt version attached to the public cache projection."""

    return f"admin_operation_cache_owner:{job_id}"


def _decoded(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def current_operation_attempt(redis, job_id: str) -> str | None:
    raw = _decoded(redis.get(operation_attempt_key(job_id)))
    return raw if isinstance(raw, str) and raw else None


async def durable_operation_attempt_is_terminal(
    job_id: str,
    publisher_attempt: str,
) -> bool:
    """Return terminal evidence only for the exact durable disk attempt.

    The Redis pointer remains the release CAS. This database read lets callers
    distinguish a valid first-cache publication gap from a genuinely terminal
    owner without holding a database row lock across Redis I/O.
    """

    from uuid import UUID

    from app.database import async_session
    from app.models.task_run import TaskRun
    from app.services.publisher_attempts import current_publisher_attempt

    try:
        task_id = UUID(job_id)
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        task = await db.get(TaskRun, task_id)
        return bool(
            task is not None
            and task.kind == "admin"
            and task.operation_type == "admin-disk-import"
            and current_publisher_attempt(task) == publisher_attempt
            and task.status in {"complete", "failed", "stale", "cancelled"}
        )


def acquire_operation_lock(
    redis,
    lock_key: str,
    job_id: str,
    *,
    ttl_seconds: int,
    publisher_attempt: str | None = None,
    replace_same_job: bool = False,
    expected_current_attempt: str | None = None,
) -> bool:
    """Acquire one single-flight owner, optionally scoped to an attempt.

    Attempt rotation may replace only the same logical TaskRun. An independent
    job remains a conflict, and the private attempt never enters the lock value
    returned by public conflict responses.
    """

    eval_script = getattr(redis, "eval", None)
    if publisher_attempt is None:
        if callable(eval_script):
            return bool(redis.eval(
                _ACQUIRE_LEGACY_OPERATION_SCRIPT,
                3,
                lock_key,
                operation_attempt_key(job_id),
                operation_cache_attempt_key(job_id),
                job_id,
                int(ttl_seconds),
            ))
        if current_operation_attempt(redis, job_id) is not None or _decoded(
            redis.get(operation_cache_attempt_key(job_id))
        ) is not None:
            return False
        return bool(redis.set(lock_key, job_id, nx=True, ex=ttl_seconds))
    if callable(eval_script):
        return bool(redis.eval(
            _ACQUIRE_ATTEMPT_OPERATION_SCRIPT,
            2,
            lock_key,
            operation_attempt_key(job_id),
            job_id,
            publisher_attempt,
            int(ttl_seconds),
            "1" if replace_same_job else "0",
            "1" if expected_current_attempt is None else "0",
            expected_current_attempt or "",
        ))
    # Compatibility for simple test/rolling clients with no EVAL method.
    # An actual EVAL failure must propagate fail-closed; check-then-write is
    # never a production fallback for immutable-attempt authority.
    current = _decoded(redis.get(lock_key))
    current_attempt = current_operation_attempt(redis, job_id)
    if current is not None and (
        current != job_id or not replace_same_job
    ):
        return False
    if current_attempt != expected_current_attempt:
        return False
    if not redis.set(lock_key, job_id, ex=int(ttl_seconds)):
        return False
    redis.set(
        operation_attempt_key(job_id),
        publisher_attempt,
        ex=int(ttl_seconds),
    )
    return True


def set_operation_status(
    job_id: str,
    status: str,
    operation_type: str,
    *,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
    publisher_attempt: str | None = None,
    redis_client=None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "operation_type": operation_type,
        "updated_at": time.time(),
    }
    if progress is not None:
        payload["progress"] = progress
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    if meta is not None:
        payload["meta"] = meta

    redis = redis_client if redis_client is not None else get_redis()
    encoded = json.dumps(payload, default=str)
    eval_script = getattr(redis, "eval", None)
    if publisher_attempt is None:
        if callable(eval_script):
            written = bool(redis.eval(
                _SET_LEGACY_OPERATION_STATUS_SCRIPT,
                3,
                operation_attempt_key(job_id),
                operation_cache_attempt_key(job_id),
                operation_key(job_id),
                OPERATION_TTL_SECONDS,
                encoded,
            ))
        else:
            if current_operation_attempt(redis, job_id) is not None or _decoded(
                redis.get(operation_cache_attempt_key(job_id))
            ) is not None:
                return None
            redis.setex(
                operation_key(job_id),
                OPERATION_TTL_SECONDS,
                encoded,
            )
            written = True
        return payload if written else None
    if callable(eval_script):
        written = bool(redis.eval(
            _SET_ATTEMPT_OPERATION_STATUS_SCRIPT,
            3,
            operation_attempt_key(job_id),
            operation_key(job_id),
            operation_cache_attempt_key(job_id),
            publisher_attempt,
            OPERATION_TTL_SECONDS,
            encoded,
        ))
    else:
        if current_operation_attempt(redis, job_id) != publisher_attempt:
            return None
        redis.setex(
            operation_key(job_id),
            OPERATION_TTL_SECONDS,
            encoded,
        )
        redis.setex(
            operation_cache_attempt_key(job_id),
            OPERATION_TTL_SECONDS,
            publisher_attempt,
        )
        written = True
    if not written:
        return None
    return payload


def get_operation_status(
    job_id: str,
    *,
    redis_client=None,
) -> dict[str, Any] | None:
    redis = redis_client if redis_client is not None else get_redis()
    eval_script = getattr(redis, "eval", None)
    if callable(eval_script):
        raw = redis.eval(
            _GET_CURRENT_OPERATION_STATUS_SCRIPT,
            3,
            operation_attempt_key(job_id),
            operation_cache_attempt_key(job_id),
            operation_key(job_id),
        )
    else:
        attempt = current_operation_attempt(redis, job_id)
        cache_attempt = _decoded(redis.get(operation_cache_attempt_key(job_id)))
        if (attempt and cache_attempt != attempt) or (
            not attempt and cache_attempt
        ):
            return None
        raw = redis.get(operation_key(job_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def release_owned_operation_lock(
    redis,
    lock_key: str,
    job_id: str,
    *,
    publisher_attempt: str | None = None,
) -> bool:
    """Delete a single-flight key only while it still belongs to ``job_id``."""

    if publisher_attempt is not None:
        eval_script = getattr(redis, "eval", None)
        if callable(eval_script):
            return bool(redis.eval(
                _RELEASE_ATTEMPT_OPERATION_SCRIPT,
                2,
                lock_key,
                operation_attempt_key(job_id),
                job_id,
                publisher_attempt,
            ))
        current = _decoded(redis.get(lock_key))
        attempt = current_operation_attempt(redis, job_id)
        if current != job_id or attempt != publisher_attempt:
            return False
        return bool(redis.delete(lock_key))

    eval_script = getattr(redis, "eval", None)
    if callable(eval_script):
        return bool(redis.eval(
            _RELEASE_LEGACY_OPERATION_SCRIPT,
            3,
            lock_key,
            operation_attempt_key(job_id),
            operation_cache_attempt_key(job_id),
            job_id,
        ))
    # Deliberate compatibility for minimal in-memory clients that have no Lua
    # executor. Production Redis errors propagate instead of degrading to a
    # check/delete race.
    if current_operation_attempt(redis, job_id) is not None or _decoded(
        redis.get(operation_cache_attempt_key(job_id))
    ) is not None:
        return False
    current = _decoded(redis.get(lock_key))
    if current != job_id:
        return False
    return bool(redis.delete(lock_key))


async def compensate_operation_enqueue_failure(
    job_id: str,
    operation_type: str,
    error: BaseException | str,
    *,
    lock_key: str | None = None,
    redis_client=None,
    publisher_attempt: str | None = None,
) -> None:
    """Make a committed admin operation terminal when RQ publication fails."""

    from app.services.publisher_attempts import redact_publisher_attempt

    message = "Queue publication failed: " + redact_publisher_attempt(
        error,
        publisher_attempt,
    )
    redis = redis_client if redis_client is not None else get_redis()

    async def compensate_task_run() -> bool:
        try:
            from uuid import UUID

            from app.database import async_session
            from app.services.publisher_attempts import (
                current_publisher_attempt,
                lock_publisher_task,
            )
            from app.services.tasks import TaskService

            async with async_session() as task_db:
                service = TaskService(task_db)
                task = (
                    await lock_publisher_task(task_db, UUID(job_id))
                    if publisher_attempt is not None
                    else await service.get(UUID(job_id))
                )
                if publisher_attempt is not None and (
                    task is None
                    or current_publisher_attempt(task) != publisher_attempt
                ):
                    await task_db.rollback()
                    return False
                if task is not None:
                    await service.update_task(
                        task,
                        status="failed",
                        progress={"phase": "failed", "label": message},
                        error=message,
                    )
                    await task_db.commit()
            return True
        except Exception:
            logger.exception(
                "Unable to compensate failed operation TaskRun job=%s",
                job_id,
            )
            return False

    # For immutable publishers the durable row is the authority boundary for
    # every compensating write, including the transient lock/status records.
    if publisher_attempt is not None:
        if not await compensate_task_run():
            return
        try:
            set_operation_status(
                job_id,
                "failed",
                operation_type,
                progress={"phase": "failed", "label": message},
                error=message,
                publisher_attempt=publisher_attempt,
                redis_client=redis,
            )
        except Exception:
            logger.warning(
                "Unable to persist failed operation Redis status job=%s",
                job_id,
                exc_info=True,
            )
        if lock_key:
            try:
                release_owned_operation_lock(
                    redis,
                    lock_key,
                    job_id,
                    publisher_attempt=publisher_attempt,
                )
            except Exception:
                logger.warning(
                    "Unable to release failed operation lock key=%s job=%s",
                    lock_key,
                    job_id,
                    exc_info=True,
                )
        return

    if lock_key:
        try:
            release_owned_operation_lock(redis, lock_key, job_id)
        except Exception:
            logger.warning(
                "Unable to release failed operation lock key=%s job=%s",
                lock_key,
                job_id,
                exc_info=True,
            )
    try:
        set_operation_status(
            job_id,
            "failed",
            operation_type,
            progress={"phase": "failed", "label": message},
            error=message,
        )
    except Exception:
        logger.warning(
            "Unable to persist failed operation Redis status job=%s",
            job_id,
            exc_info=True,
        )
    await compensate_task_run()


async def enqueue_admin_operation(
    *,
    lock_key: str,
    operation_type: str,
    title: str,
    entity: str,
    func: str,
    options: dict[str, Any] | None = None,
    job_timeout: int = 14400,
    queue_name: str = "operations",
) -> dict[str, Any]:
    """Enqueue a long-running admin operation on a governed worker queue.

    Shared plumbing for every batch endpoint: single-flight redis lock (stale
    locks from finished jobs are reclaimed), a task row for the task center,
    the redis operation-status record, and the RQ enqueue. Batch work must
    never run inline in the backend process — it belongs in a worker.

    Raises HTTPException(409) when the same operation is already running.
    """
    import uuid as _uuid
    from uuid import UUID as _UUID

    from fastapi import HTTPException
    from rq import Queue

    from app.database import async_session
    from app.services.tasks import TaskService

    options = dict(options or {})
    if queue_name not in {"operations", "imports", "maintenance"}:
        raise ValueError(f"Unsupported admin operation queue: {queue_name}")
    redis = get_redis()
    ensure_redis_enqueue_capacity(redis)
    job_id = str(_uuid.uuid4())

    active_job = redis.get(lock_key)
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            # The status read and reclamation are necessarily separate.  A
            # second request may install a new owner between them, so reclaim
            # only the owner we actually inspected.
            release_owned_operation_lock(redis, lock_key, active_job)
    lock_ttl = max(OPERATION_TTL_SECONDS, int(job_timeout) + 3600)
    if not redis.set(lock_key, job_id, nx=True, ex=lock_ttl):
        active_job = redis.get(lock_key)
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={
            "message": f"{title} already running", "job_id": active_job})

    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=_UUID(job_id),
            kind="admin",
            operation_type=operation_type,
            title=title,
            status="enqueued",
            queue_name=queue_name,
            progress={"phase": "enqueued", "label": f"{title} queued"},
            meta={"entity": entity, **options},
        )
        await task_db.commit()
    try:
        set_operation_status(job_id, "enqueued", operation_type,
            progress={"phase": "enqueued", "label": f"{title} queued"},
            meta={"entity": entity, **options})
        rq_job = checked_enqueue(
            Queue(name=queue_name, connection=redis),
            func,
            job_id,
            options,
            job_timeout=job_timeout,
            result_ttl=604800,
        )
    except Exception as exc:
        await compensate_operation_enqueue_failure(
            job_id,
            operation_type,
            exc,
            lock_key=lock_key,
            redis_client=redis,
        )
        raise
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id}
