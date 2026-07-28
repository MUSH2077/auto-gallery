"""Shared status storage for background admin operations."""

from __future__ import annotations

import json
import time
from typing import Any

from app.services.redis_client import get_redis

OPERATION_TTL_SECONDS = 7200


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


async def enqueue_admin_operation(
    *,
    lock_key: str,
    operation_type: str,
    title: str,
    entity: str,
    func: str,
    options: dict[str, Any] | None = None,
    job_timeout: int = 14400,
) -> dict[str, Any]:
    """Enqueue a long-running admin operation on the `operations` queue.

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
    redis = get_redis()
    job_id = str(_uuid.uuid4())

    active_job = redis.get(lock_key)
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete(lock_key)
    if not redis.set(lock_key, job_id, nx=True, ex=604800):
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
            queue_name="operations",
            progress={"phase": "enqueued", "label": f"{title} queued"},
            meta={"entity": entity, **options},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", operation_type,
        progress={"phase": "enqueued", "label": f"{title} queued"},
        meta={"entity": entity, **options})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        func, job_id, options, job_timeout=job_timeout, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id}
