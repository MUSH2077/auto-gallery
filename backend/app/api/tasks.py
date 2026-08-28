import logging
import mimetypes
import os
from datetime import datetime
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdminUser, RequirePermission, get_admin_key
from app.database import get_db
from app.services.operations import (
    admin_operation_permissions_for_user,
    inaccessible_admin_operation_types,
    get_operation_status,
    require_admin_operation_access,
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
    restore_missed_subscription_slot,
)

_require_tasks = RequirePermission("tasks")
router = APIRouter(dependencies=[_require_tasks])
logger = logging.getLogger(__name__)


def _stream_conflict_media(handle):
    try:
        while chunk := handle.read(1024 * 1024):
            yield chunk
    finally:
        handle.close()


class ReconcileTasksRequest(BaseModel):
    dry_run: bool = True
    limit: int = Field(500, ge=1, le=2000)


class CompactTasksRequest(BaseModel):
    dry_run: bool = True
    limit: int = Field(200, ge=1, le=1000)


class RestoreSubscriptionSlotRequest(BaseModel):
    slot_at: datetime
    source_ids: list[UUID] = Field(min_length=1, max_length=500)
    dry_run: bool = True


class ConflictDecision(BaseModel):
    relative_path: str = Field(min_length=1, max_length=2000)
    winner: Literal["canonical", "staged"]


class ResolveDownloadConflictRequest(BaseModel):
    decisions: list[ConflictDecision] = Field(min_length=1, max_length=500)
    resolution_id: str | None = Field(default=None, max_length=128)


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
    user=_require_tasks,
):
    excluded_operation_types = inaccessible_admin_operation_types(user)
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
            excluded_admin_operation_types=excluded_operation_types,
            user_id=user.id,
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
            permissions=admin_operation_permissions_for_user(user),
            user_id=user.id,
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    return result


@router.get("/anomalies")
async def list_task_anomalies(
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user=_require_tasks,
):
    """Compatibility endpoint for the unified attention view."""

    return await operations_overview(
        db,
        view="attention",
        offset=max(0, offset),
        limit=max(1, min(limit, 100)),
        excluded_admin_operation_types=inaccessible_admin_operation_types(user),
        user_id=user.id,
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


@router.post("/reconcile-subscription-slot", dependencies=[RequirePermission("system")])
async def reconcile_subscription_slot(
    data: RestoreSubscriptionSlotRequest,
    db: AsyncSession = Depends(get_db),
):
    return await restore_missed_subscription_slot(
        db,
        slot_at=data.slot_at,
        source_ids=data.source_ids,
        dry_run=data.dry_run,
    )


@router.get("/{task_id}")
async def get_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=_require_tasks,
):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not await svc.is_visible_to_user(task, user.id):
        raise HTTPException(status_code=404, detail="Task not found")
    if task.kind == "admin":
        require_admin_operation_access(user, task.operation_type)
    events = await svc.task_events(task_id)
    return task_payload(task, events)


@router.get("/{task_id}/conflicts")
async def get_download_conflicts(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    from app.services.download_conflicts import DownloadConflictError, DownloadConflictService

    try:
        return await DownloadConflictService(db).inspect(task_id)
    except DownloadConflictError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/{task_id}/conflicts/media")
async def get_download_conflict_media(
    task_id: UUID,
    relative_path: str = Query(min_length=1, max_length=2000),
    side: Literal["canonical", "staged"] = Query(),
    db: AsyncSession = Depends(get_db),
):
    from app.services.download_conflicts import DownloadConflictError, DownloadConflictService

    try:
        handle, stored_relative = await DownloadConflictService(db).open_media(
            task_id,
            relative_path,
            side,
        )
    except DownloadConflictError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    size = os.fstat(handle.fileno()).st_size
    media_type = mimetypes.guess_type(stored_relative)[0] or "application/octet-stream"
    return StreamingResponse(
        _stream_conflict_media(handle),
        media_type=media_type,
        headers={"Content-Length": str(size)},
    )


@router.post("/{task_id}/conflicts/resolve")
async def resolve_download_conflicts(
    task_id: UUID,
    data: ResolveDownloadConflictRequest,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    from app.services.download_conflicts import DownloadConflictError, DownloadConflictService

    decisions = {item.relative_path: item.winner for item in data.decisions}
    if len(decisions) != len(data.decisions):
        raise HTTPException(status_code=422, detail="Each conflict path must appear exactly once")
    try:
        result = await DownloadConflictService(db).resolve(
            task_id,
            decisions,
            operator=operator,
            resolution_id=data.resolution_id,
        )
        await db.commit()
        if result.get("idempotent_replay"):
            result["retry"] = {"status": "already_resolved"}
            return result
        try:
            retry = await TaskEngine(db).retry_download(
                UUID(str(result["download_job_id"])),
                operator=operator,
            )
            await db.commit()
            result["retry"] = retry
        except Exception as retry_exc:
            await db.rollback()
            logger.exception(
                "Conflict resolution saved but download retry scheduling failed",
                extra={"task_id": str(task_id)},
            )
            result["retry"] = {
                "status": "needs_retry",
                "message": (
                    "Conflict resolution was saved, but the download retry "
                    "could not be scheduled"
                ),
            }
        return result
    except DownloadConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{task_id}/conflicts/resolutions/{resolution_id}/rollback")
async def rollback_download_conflict_resolution(
    task_id: UUID,
    resolution_id: str,
    _admin=RequireAdminUser,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    from app.services.download_conflicts import DownloadConflictError, DownloadConflictService

    try:
        result = await DownloadConflictService(db).rollback(
            task_id,
            resolution_id,
            operator=operator,
        )
        await db.commit()
        return result
    except DownloadConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{task_id}/acknowledge")
async def acknowledge_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
    user=_require_tasks,
):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.kind == "admin":
        require_admin_operation_access(user, task.operation_type)
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
    user=None,
):
    svc = TaskService(db)
    task = await svc.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.kind == "admin":
        require_admin_operation_access(user, task.operation_type)
        if action != "retry":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "invalid_task_action",
                    "action": action,
                    "message": "This admin task only supports retry",
                },
            )
        return await _retry_admin_task(task, svc)
    if not task.subject_id or task.subject_type not in {"download_job", "import_job"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invalid_task_action",
                "action": action,
                "message": "This task type does not support direct control yet",
            },
        )

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
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invalid_task_action",
                "action": action,
                "message": str(exc),
            },
        ) from exc


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
    "danbooru-mapping-refresh": (
        "library:creator-reenrich:active",
        "app.jobs.admin_operations.run_creator_reenrich_operation",
        "creators",
        "Danbooru mapping refresh queued",
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
    "hierarchy-delete": (
        "library:hierarchy-delete:active",
        "app.jobs.admin_operations.run_hierarchy_delete_operation",
        "hierarchy-delete",
        "Permanent deletion queued",
    ),
    "asset-dedup-scan": (
        "lock:admin:asset-dedup-scan",
        "app.jobs.asset_dedup.run_asset_dedup_scan",
        "assets",
        "Asset dedup scan queued",
    ),
}


async def _retry_admin_task(task, svc: TaskService):
    if isinstance((task.meta or {}).get("admin_dispatch"), dict):
        from app.services.operations import retry_admin_operation

        task_id = task.id
        await svc.db.rollback()
        return await retry_admin_operation(task_id)

    raise HTTPException(
        status_code=400,
        detail=(
            "This legacy admin operation is available for read-only "
            "compatibility and cannot be retried"
        ),
    )

    from rq import Queue

    from app.models.task_run import TaskRun

    with svc.db.no_autoflush:
        task = (
            await svc.db.execute(
                select(TaskRun)
                .where(TaskRun.id == task.id)
                .with_for_update(of=TaskRun)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    logical_task_id = task.id
    operation_type = task.operation_type

    spec = _RETRYABLE_ADMIN_OPERATIONS.get(operation_type)
    if spec is None:
        raise HTTPException(status_code=400, detail="This admin operation cannot be retried")
    if task.status not in {"failed", "stale", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invalid_task_action",
                "action": "retry",
                "status": task.status,
                "message": f"Task is {task.status}; retry is only available after failure",
            },
        )

    is_disk_publisher = operation_type == "admin-disk-import"
    if is_disk_publisher:
        # Redis admission can wait on host/network state. Do not retain the
        # TaskRun row lock while inspecting or reclaiming operational state;
        # reacquire and revalidate immediately before rotating the attempt.
        await svc.db.rollback()

    redis = get_redis()
    ensure_redis_enqueue_capacity(redis)
    lock_key, func, entity, label = spec
    retry_job_timeout = (
        7 * 24 * 60 * 60
        if operation_type in {
            "admin-search-reindex",
            "admin-curation-backfill",
            "hierarchy-delete",
        }
        else 3600
        if operation_type == "asset-dedup-scan"
        else 14400
    )

    active_job = redis.get(lock_key)
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if is_disk_publisher:
            if active_job != str(logical_task_id):
                from app.services.operations import (
                    current_operation_attempt,
                    durable_operation_attempt_is_terminal,
                )

                active_attempt = current_operation_attempt(redis, active_job)
                reclaimable = (
                    active_status
                    and active_status.get("status") in {
                        "complete",
                        "failed",
                        "cancelled",
                        "stale",
                    }
                ) or (not active_status and active_attempt is None)
                if (
                    not reclaimable
                    and not active_status
                    and active_attempt is not None
                ):
                    reclaimable = await durable_operation_attempt_is_terminal(
                        active_job,
                        active_attempt,
                    )
                if reclaimable:
                    release_owned_operation_lock(
                        redis,
                        lock_key,
                        active_job,
                        **(
                            {"publisher_attempt": active_attempt}
                            if active_attempt is not None
                            else {}
                        ),
                    )
                else:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Operation already running",
                            "job_id": active_job,
                        },
                    )
            # The same logical TaskRun is atomically handed from A to B only
            # after B's durable attempt commits below.
        elif not active_status or active_status.get("status") in {
            "complete",
            "failed",
            "cancelled",
            "stale",
        }:
            release_owned_operation_lock(redis, lock_key, active_job)
        elif active_job != str(task.id):
            raise HTTPException(status_code=409, detail={"message": "Operation already running", "job_id": active_job})

    if not is_disk_publisher and not redis.set(
        lock_key,
        str(task.id),
        nx=True,
        ex=max(8 * 24 * 60 * 60, retry_job_timeout + 3600),
    ):
        active_job = redis.get(lock_key)
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Operation already running", "job_id": active_job})

    if is_disk_publisher:
        from app.services.publisher_attempts import lock_publisher_task

        task = await lock_publisher_task(svc.db, logical_task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status not in {"failed", "stale", "cancelled"}:
            await svc.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "invalid_task_action",
                    "action": "retry",
                    "status": task.status,
                    "message": (
                        f"Task is {task.status}; retry is only available "
                        "after failure"
                    ),
                },
            )

    from app.services.publisher_attempts import public_task_meta

    meta = dict(public_task_meta(task.meta) or {})
    options = {k: v for k, v in meta.items() if k != "entity"}
    dedup_retry: tuple[UUID, int] | None = None
    if operation_type == "asset-dedup-scan":
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
    publisher_attempt = None
    if is_disk_publisher:
        from app.services.publisher_attempts import (
            PUBLISHER_ATTEMPT_META_KEY,
            current_publisher_attempt,
            ensure_publisher_attempt,
        )
        from app.services.operations import current_operation_attempt

        previous_publisher_attempt = current_publisher_attempt(task)
        expected_operational_attempt = current_operation_attempt(
            redis,
            str(task.id),
        )
        if expected_operational_attempt not in {
            None,
            previous_publisher_attempt,
        }:
            await svc.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "publisher_attempt_conflict",
                    "message": "Disk import operational attempt is inconsistent",
                },
            )

        publisher_attempt, _rotated = ensure_publisher_attempt(
            task,
            rotate=True,
        )
        task.queue_name = "maintenance"
        await svc.update_task(
            task,
            status="enqueued",
            progress=progress,
            result={},
            error="",
            meta={
                "entity": entity,
                **options,
                PUBLISHER_ATTEMPT_META_KEY: publisher_attempt,
            },
        )
        # The immutable attempt is an outbox boundary: it must be durable
        # before Redis/RQ can expose the exact captured token to a worker.
        await svc.db.commit()
        from app.services.operations import acquire_operation_lock

        if not acquire_operation_lock(
            redis,
            lock_key,
            str(task.id),
            ttl_seconds=max(
                8 * 24 * 60 * 60,
                retry_job_timeout + 3600,
            ),
            publisher_attempt=publisher_attempt,
            replace_same_job=True,
            expected_current_attempt=expected_operational_attempt,
        ):
            from app.services.publisher_attempts import current_publisher_attempt

            await svc.db.rollback()
            current = await lock_publisher_task(svc.db, logical_task_id)
            if (
                current is not None
                and current_publisher_attempt(current) == publisher_attempt
            ):
                await svc.update_task(
                    current,
                    status="failed",
                    progress={
                        "phase": "failed",
                        "label": (
                            "Disk import retry lost single-flight admission"
                        ),
                    },
                    error="Disk import retry lost single-flight admission",
                    reason_code="operation_conflict",
                )
                await svc.db.commit()
            else:
                await svc.db.rollback()
            active_job = redis.get(lock_key)
            if isinstance(active_job, bytes):
                active_job = active_job.decode()
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Operation already running",
                    "job_id": active_job,
                },
            )
        set_operation_status(
            str(task.id),
            "enqueued",
            operation_type,
            progress=progress,
            meta={"entity": entity, **options},
            publisher_attempt=publisher_attempt,
            redis_client=redis,
        )
    try:
        worker_args = (
            (str(task.id), options, publisher_attempt)
            if publisher_attempt is not None
            else (str(task.id), options)
        )
        description_kwargs = {}
        if publisher_attempt is not None:
            from app.services.publisher_attempts import publisher_job_description

            description_kwargs["description"] = publisher_job_description(
                task.id,
            )
        rq_job = checked_enqueue(
            Queue(
                # All long/admin coordinators now run on the governed
                # maintenance listener. Never trust a pre-upgrade TaskRun's
                # operations/imports queue during retry.
                name="maintenance",
                connection=redis,
            ),
            func,
            *worker_args,
            job_timeout=retry_job_timeout,
            result_ttl=604800,
            **description_kwargs,
        )
    except Exception as exc:
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
        if publisher_attempt is not None:
            from app.services.publisher_attempts import (
                current_publisher_attempt,
                lock_publisher_task,
            )

            failed_current_attempt = False
            await svc.db.rollback()
            current = await lock_publisher_task(svc.db, logical_task_id)
            if (
                current is not None
                and current_publisher_attempt(current) == publisher_attempt
            ):
                await svc.update_task(
                    current,
                    status="failed",
                    progress={
                        "phase": "failed",
                        "label": "Disk import queue publication failed",
                    },
                    error="Disk import queue publication failed",
                    reason_code="queue_publication_failed",
                )
                await svc.db.commit()
                failed_current_attempt = True
            else:
                await svc.db.rollback()
            if failed_current_attempt:
                set_operation_status(
                    str(logical_task_id),
                    "failed",
                    "admin-disk-import",
                    progress={
                        "phase": "failed",
                        "label": "Disk import queue publication failed",
                    },
                    error="Disk import queue publication failed",
                    meta={"entity": entity, **options},
                    publisher_attempt=publisher_attempt,
                    redis_client=redis,
                )
        try:
            release_owned_operation_lock(
                redis,
                lock_key,
                str(logical_task_id),
                **(
                    {"publisher_attempt": publisher_attempt}
                    if publisher_attempt is not None
                    else {}
                ),
            )
        except Exception:
            # The task remains terminal, so a later retry can safely reclaim a
            # stale lock once Redis is reachable again.
            pass
        if publisher_attempt is not None:
            from app.services.publisher_attempts import redact_publisher_attempt

            safe_error = redact_publisher_attempt(exc, publisher_attempt)
            if safe_error != str(exc):
                raise RuntimeError(safe_error) from None
        raise
    if publisher_attempt is not None:
        from app.services.publisher_attempts import (
            current_publisher_attempt,
            lock_publisher_task,
        )

        current = await lock_publisher_task(svc.db, logical_task_id)
        if (
            current is None
            or current_publisher_attempt(current) != publisher_attempt
            or current.status != "enqueued"
        ):
            await svc.db.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "publisher_attempt_superseded",
                    "message": "Disk import retry was superseded before publication",
                },
            )
        task = current
        await svc.update_task(task, rq_job_id=rq_job.id)
        await svc.db.commit()
    else:
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
        operation_type,
        progress=progress,
        meta={"entity": entity, **options},
        publisher_attempt=publisher_attempt,
        redis_client=(redis if publisher_attempt is not None else None),
    )
    return {"task_id": str(task.id), "job_id": rq_job.id, "status": "enqueued"}


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
    user=_require_tasks,
):
    return await _control_task(task_id, "retry", db, operator, user=user)


@router.post("/{task_id}/pause")
async def pause_task(
    task_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
    user=_require_tasks,
):
    return await _control_task(
        task_id,
        "pause",
        db,
        operator,
        note=(data or {}).get("note"),
        user=user,
    )


@router.post("/{task_id}/resume")
async def resume_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
    user=_require_tasks,
):
    return await _control_task(task_id, "resume", db, operator, user=user)


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
    user=_require_tasks,
):
    return await _control_task(
        task_id,
        "cancel",
        db,
        operator,
        note=(data or {}).get("note"),
        user=user,
    )
