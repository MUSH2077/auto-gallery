from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission, get_admin_key
from app.database import get_db
from app.services.operations import (
    get_operation_status,
    release_owned_operation_lock,
    set_operation_status,
)
from app.services.redis_client import get_redis
from app.services.queue_admission import checked_enqueue, ensure_redis_enqueue_capacity
from app.services.search import SearchService
from app.services.search_language import SearchQueryError, compose_search_query
from app.services.backpressure import DownloadAdmissionError
from app.services.task_engine import TaskEngine, TaskEngineError
from app.services.tasks import TaskService, task_payload
from app.services.operation_attention import (
    compact_terminal_tasks,
    operations_overview,
    reconcile_task_truth,
)

router = APIRouter(dependencies=[RequirePermission("tasks")])


class ReconcileTasksRequest(BaseModel):
    dry_run: bool = True
    limit: int = Field(500, ge=1, le=2000)


class CompactTasksRequest(BaseModel):
    dry_run: bool = True
    limit: int = Field(200, ge=1, le=1000)


@router.get("")
async def list_tasks(
    kind: str | None = None,
    status: str | None = None,
    operation_type: str | None = None,
    source: str | None = None,
    q: str | None = None,
    include_account: bool = False,
    visibility: Literal["actionable", "all"] = "all",
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
            visibility=visibility,
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
        result = await SearchService(db).search_tasks(
            canonical,
            offset=offset,
            limit=limit,
            visibility=visibility,
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    return result


@router.get("/anomalies")
async def list_task_anomalies(
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Compatibility endpoint for the unified attention view."""

    return await operations_overview(
        db,
        view="attention",
        offset=max(0, offset),
        limit=max(1, min(limit, 100)),
    )


@router.post("/reconcile")
async def reconcile_tasks(
    data: ReconcileTasksRequest,
    db: AsyncSession = Depends(get_db),
):
    return await reconcile_task_truth(db, dry_run=data.dry_run, limit=data.limit)


@router.post("/compact", dependencies=[RequirePermission("system")])
async def compact_tasks(
    data: CompactTasksRequest,
    db: AsyncSession = Depends(get_db),
):
    return await compact_terminal_tasks(db, dry_run=data.dry_run, limit=data.limit)


@router.get("/{task_id}")
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    events = await svc.task_events(task_id)
    return task_payload(task, events)


@router.post("/{task_id}/acknowledge")
async def acknowledge_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.attention_state not in {"open", "resolved"}:
        raise HTTPException(status_code=409, detail="Task is not an actionable anomaly")
    await svc.update_task(task, attention_state="acknowledged")
    await svc.add_event(
        task,
        "acknowledged",
        to_status=task.status,
        message="Anomaly acknowledged",
        payload={"operator": operator},
    )
    await db.commit()
    return task_payload(task)


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
    except DownloadAdmissionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.payload()) from exc
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
    "admin-search-reindex": (
        "library:search-reindex:active",
        "app.jobs.admin_operations.run_search_reindex_operation",
        "search-reindex",
        "Search reindex queued",
    ),
    "admin-gitllery-sync": (
        "library:gitllery-sync:active",
        "app.jobs.admin_operations.run_gitllery_sync_operation",
        "gitllery-sync",
        "Gitllery sync queued",
    ),
    "admin-curation-backfill": (
        "library:curation-backfill:active",
        "app.jobs.admin_operations.run_curation_backfill_operation",
        "curation-backfill",
        "Curation baseline backfill queued",
    ),
    "asset-dedup-scan": (
        "lock:admin:asset-dedup-scan",
        "app.jobs.asset_dedup.run_asset_dedup_scan",
        "assets",
        "Asset dedup scan queued",
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
    ensure_redis_enqueue_capacity(redis)
    lock_key, func, entity, label = spec
    retry_job_timeout = (
        7 * 24 * 60 * 60
        if task.operation_type in {
            "admin-search-reindex",
            "admin-curation-backfill",
        }
        else 3600
        if task.operation_type == "asset-dedup-scan"
        else 14400
    )

    active_job = redis.get(lock_key)
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled", "stale"}:
            release_owned_operation_lock(redis, lock_key, active_job)
        elif active_job != str(task.id):
            raise HTTPException(status_code=409, detail={"message": "Operation already running", "job_id": active_job})

    if not redis.set(
        lock_key,
        str(task.id),
        nx=True,
        ex=max(8 * 24 * 60 * 60, retry_job_timeout + 3600),
    ):
        active_job = redis.get(lock_key)
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Operation already running", "job_id": active_job})

    meta = dict(task.meta or {})
    options = {k: v for k, v in meta.items() if k != "entity"}
    dedup_retry: tuple[UUID, int] | None = None
    if task.operation_type == "asset-dedup-scan":
        from app.models import AssetDedupScan

        try:
            scan_id = UUID(str(options["scan_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            release_owned_operation_lock(redis, lock_key, str(task.id))
            raise HTTPException(
                status_code=409,
                detail="Asset dedup scan retry metadata is invalid",
            ) from exc
        scan = (
            await svc.db.execute(
                select(AssetDedupScan)
                .where(AssetDedupScan.id == scan_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if scan is None or scan.status == "complete":
            release_owned_operation_lock(redis, lock_key, str(task.id))
            raise HTTPException(
                status_code=409,
                detail="Asset dedup scan is missing or already complete",
            )
        scan_options = dict(scan.options or {})
        try:
            generation = max(0, int(scan_options.get("_rq_generation", 0)))
        except (TypeError, ValueError):
            generation = 0
        scan_options["_operation_job_id"] = str(task.id)
        scan_options.pop("_next_rq_job_id", None)
        scan.options = scan_options
        scan.status = "pending"
        scan.error = None
        await svc.db.commit()
        options["_scan_generation"] = generation
        dedup_retry = (scan_id, generation)
    progress = {"phase": "enqueued", "label": label}
    try:
        rq_job = checked_enqueue(
            Queue(
                # All long/admin coordinators now run on the governed
                # maintenance listener. Never trust a pre-upgrade TaskRun's
                # operations/imports queue during retry.
                name="maintenance",
                connection=redis,
            ),
            func,
            str(task.id),
            options,
            job_timeout=retry_job_timeout,
            result_ttl=604800,
        )
    except Exception:
        if dedup_retry is not None:
            from app.models import AssetDedupScan

            scan_id, generation = dedup_retry
            try:
                scan = (
                    await svc.db.execute(
                        select(AssetDedupScan)
                        .where(AssetDedupScan.id == scan_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if scan is not None:
                    current_options = dict(scan.options or {})
                    try:
                        current_generation = int(
                            current_options.get("_rq_generation", 0)
                        )
                    except (TypeError, ValueError):
                        current_generation = -1
                    if current_generation == generation:
                        scan.status = "failed"
                        scan.error = "Unable to enqueue asset dedup scan retry"
                        await svc.db.commit()
            except Exception:
                await svc.db.rollback()
        try:
            release_owned_operation_lock(redis, lock_key, str(task.id))
        except Exception:
            # The task remains terminal, so a later retry can safely reclaim a
            # stale lock once Redis is reachable again.
            pass
        raise
    task.queue_name = "maintenance"
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
