from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission, get_admin_key
from app.database import get_db
from app.services.operations import get_operation_status, set_operation_status
from app.services.redis_client import get_redis
from app.services.search import SearchService
from app.services.search_language import SearchQueryError, compose_search_query
from app.services.task_engine import TaskEngine, TaskEngineError
from app.services.tasks import TaskService, task_payload

router = APIRouter(dependencies=[RequirePermission("tasks")])


@router.get("")
async def list_tasks(
    kind: str | None = None,
    status: str | None = None,
    operation_type: str | None = None,
    source: str | None = None,
    q: str | None = None,
    include_account: bool = False,
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    if include_account:
        svc = TaskService(db)
        total, tasks = await svc.list_tasks(
            kind=kind,
            status=status,
            operation_type=operation_type,
            source=source,
            include_account=True,
            offset=offset,
            limit=limit,
        )
        return {"total": total, "items": [task_payload(task) for task in tasks]}
    canonical = q or ""
    for key, value in (("kind", kind), ("status", status), ("source", source)):
        if value:
            canonical = compose_search_query(
                canonical,
                "tasks",
                key=key,
                value=value,
                operation="add",
            ).canonical
    if operation_type:
        canonical = f'{canonical} "{operation_type}"'.strip()
    try:
        result = await SearchService(db).search(
            canonical,
            offset=offset,
            limit=limit,
            scope="tasks",
            permissions={"tasks"},
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    return result["groups"]["tasks"]


@router.get("/{task_id}")
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    events = await svc.task_events(task_id)
    return task_payload(task, events)


async def _control_task(
    task_id: UUID,
    action: str,
    db: AsyncSession,
    operator: str,
    note: str | None = None,
):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.kind == "admin":
        if action != "retry":
            raise HTTPException(status_code=400, detail="This admin task only supports retry")
        return await _retry_admin_task(task, svc)
    if not task.subject_id or task.subject_type not in {"download_job", "import_job"}:
        raise HTTPException(status_code=400, detail="This task type does not support direct control yet")

    engine = TaskEngine(db)
    try:
        if task.subject_type == "download_job":
            if action == "retry":
                result = await engine.retry_download(task.subject_id, operator=operator)
            elif action == "pause":
                result = await engine.pause_download(task.subject_id, note=note, operator=operator)
            elif action == "resume":
                result = await engine.resume_download(task.subject_id, operator=operator)
            elif action == "cancel":
                result = await engine.cancel_download(task.subject_id, note=note, operator=operator)
            else:
                raise HTTPException(status_code=400, detail="Unknown action")
            await svc.update_task(task, status=result.get("status"))
        else:
            if action == "retry":
                result = await engine.retry_import(task.subject_id, operator=operator)
            elif action == "pause":
                result = await engine.pause_import(task.subject_id, note=note, operator=operator)
            elif action == "resume":
                result = await engine.resume_import(task.subject_id, operator=operator)
            elif action == "cancel":
                result = await engine.cancel_import(task.subject_id, note=note, operator=operator)
            else:
                raise HTTPException(status_code=400, detail="Unknown action")
            await svc.update_task(task, status=result.get("status"))
        await db.commit()
        return {"task_id": str(task.id), **result}
    except TaskEngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# operation_type → (lock_key, func, entity, label). Retry re-enqueues the same
# job function with the task's stored options (meta minus "entity"), so every
# entry here must keep its options round-trippable through task.meta.
_RETRYABLE_ADMIN_OPERATIONS = {
    "admin-disk-import": (
        "library:disk-import:active",
        "app.jobs.admin_operations.run_disk_import_operation",
        "disk-import",
        "Disk import queued",
    ),
    "admin-creator-reenrich": (
        "library:creator-reenrich:active",
        "app.jobs.admin_operations.run_creator_reenrich_operation",
        "creator-reenrich",
        "Creator re-enrichment queued",
    ),
    "admin-rebuild": (
        "library:rebuild:active",
        "app.jobs.admin_operations.run_library_rebuild_operation",
        "library",
        "Library rebuild queued",
    ),
    "admin-gitllery-sync": (
        "library:gitllery-sync:active",
        "app.jobs.admin_operations.run_gitllery_sync_operation",
        "gitllery-sync",
        "Gitllery sync queued",
    ),
    "admin-search-reindex": (
        "library:search-reindex:active",
        "app.jobs.admin_operations.run_search_reindex_operation",
        "search-reindex",
        "Search reindex queued",
    ),
    "admin-curation-backfill": (
        "library:curation-backfill:active",
        "app.jobs.admin_operations.run_curation_backfill_operation",
        "curation-backfill",
        "Curation baseline backfill queued",
    ),
}


async def _retry_admin_task(task, svc: TaskService):
    from rq import Queue

    spec = _RETRYABLE_ADMIN_OPERATIONS.get(task.operation_type)
    if spec is None:
        raise HTTPException(status_code=400, detail="This admin operation cannot be retried")
    if task.status not in {"failed", "stale", "cancelled"}:
        raise HTTPException(status_code=409, detail=f"Task is {task.status}; retry is only available after failure")

    redis = get_redis()
    lock_key, func, entity, label = spec

    active_job = redis.get(lock_key)
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled", "stale"}:
            redis.delete(lock_key)
        elif active_job != str(task.id):
            raise HTTPException(status_code=409, detail={"message": "Operation already running", "job_id": active_job})

    if not redis.set(lock_key, str(task.id), nx=True, ex=604800):
        active_job = redis.get(lock_key)
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Operation already running", "job_id": active_job})

    meta = dict(task.meta or {})
    options = {k: v for k, v in meta.items() if k != "entity"}
    progress = {"phase": "enqueued", "label": label}
    rq_job = Queue(name="operations", connection=redis).enqueue(
        func,
        str(task.id),
        options,
        job_timeout=14400,
        result_ttl=604800,
    )
    await svc.update_task(
        task,
        status="enqueued",
        progress=progress,
        result={},
        error="",
        meta={"entity": entity, **options},
        rq_job_id=rq_job.id,
    )
    await svc.db.commit()
    set_operation_status(
        str(task.id),
        "enqueued",
        task.operation_type,
        progress=progress,
        meta={"entity": entity, **options},
    )
    return {"task_id": str(task.id), "job_id": rq_job.id, "status": "enqueued"}


@router.post("/{task_id}/retry")
async def retry_task(task_id: UUID, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "retry", db, operator)


@router.post("/{task_id}/pause")
async def pause_task(task_id: UUID, data: dict | None = None, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "pause", db, operator, note=(data or {}).get("note"))


@router.post("/{task_id}/resume")
async def resume_task(task_id: UUID, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "resume", db, operator)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: UUID, data: dict | None = None, db: AsyncSession = Depends(get_db), operator: str = Depends(get_admin_key)):
    return await _control_task(task_id, "cancel", db, operator, note=(data or {}).get("note"))
