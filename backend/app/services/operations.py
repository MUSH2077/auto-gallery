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


def operation_key(job_id: str) -> str:
    return f"admin_operation:{job_id}"


def set_operation_status(
    job_id: str,
    status: str,
    operation_type: str,
    *,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
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

    get_redis().setex(
        operation_key(job_id),
        OPERATION_TTL_SECONDS,
        json.dumps(payload, default=str),
    )
    return payload


def get_operation_status(job_id: str) -> dict[str, Any] | None:
    raw = get_redis().get(operation_key(job_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def release_owned_operation_lock(redis, lock_key: str, job_id: str) -> bool:
    """Delete a single-flight key only while it still belongs to ``job_id``."""

    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        return bool(redis.eval(script, 1, lock_key, job_id))
    except Exception:
        # Some test/fallback clients do not implement EVAL. Preserve ownership
        # checking in the non-atomic fallback; never delete a newer job's lock.
        current = redis.get(lock_key)
        if isinstance(current, bytes):
            current = current.decode()
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
) -> None:
    """Make a committed admin operation terminal when RQ publication fails."""

    message = f"Queue publication failed: {error}"
    redis = redis_client or get_redis()
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

    try:
        from uuid import UUID

        from app.database import async_session
        from app.services.tasks import TaskService

        async with async_session() as task_db:
            service = TaskService(task_db)
            task = await service.get(UUID(job_id))
            if task is not None:
                await service.update_task(
                    task,
                    status="failed",
                    progress={"phase": "failed", "label": message},
                    error=message,
                )
                await task_db.commit()
    except Exception:
        logger.exception("Unable to compensate failed operation TaskRun job=%s", job_id)


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
