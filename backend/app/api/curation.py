from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.schemas.curation import (
    CurationBackfillRunResponse,
    CurationBackfillStatusResponse,
    CurationCommitListResponse,
    CurationCommitRead,
    CurationRevertResponse,
    PurgePreviewRequest,
    PurgePreviewResponse,
    PurgeRequest,
    RuleSuggestionRead,
)
from app.schemas.gitllery import (
    GitlleryLogResponse, GitlleryReconcileResponse, GitlleryRebuildResponse, GitlleryStatusResponse,
)
from app.services.curation import CurationService
from app.services.gitllery import GitlleryService, project_commit_safe

router = APIRouter(dependencies=[RequirePermission("curation")])


@router.get("/commits", response_model=CurationCommitListResponse)
async def list_curation_commits(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    subject_type: str | None = None,
    subject_id: str | None = None,
    trigger: str | None = None,
    include_baseline: bool = True,
    db: AsyncSession = Depends(get_db),
):
    svc = CurationService(db)
    return await svc.list_commits(
        offset=offset,
        limit=limit,
        subject_type=subject_type,
        subject_id=subject_id,
        trigger=trigger,
        include_baseline=include_baseline,
    )


@router.get("/backfill/status", response_model=CurationBackfillStatusResponse)
async def curation_backfill_status(db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.backfill_status()


@router.post("/backfill")
async def run_curation_backfill():
    """Enqueue the baseline backfill — it replays the whole library and must
    run in a worker, never inline in the backend process."""
    from app.services.operations import enqueue_admin_operation
    return await enqueue_admin_operation(
        lock_key="library:curation-backfill:active",
        operation_type="admin-curation-backfill",
        title="Curation baseline backfill",
        entity="curation-backfill",
        func="app.jobs.admin_operations.run_curation_backfill_operation",
    )


@router.get("/commits/{commit_id}", response_model=CurationCommitRead)
async def get_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.commit_payload(commit_id)


@router.post("/commits/{commit_id}/revert", response_model=CurationRevertResponse)
async def revert_curation_commit(commit_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    result = await svc.revert_commit(commit_id)
    if result.get("commit") is not None:
        await project_commit_safe(db, result["commit"].id)
    return result


@router.post("/purge/preview", response_model=PurgePreviewResponse)
async def preview_purge(data: PurgePreviewRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.purge_preview(data.work_ids)


@router.post("/purge", response_model=CurationCommitRead)
async def purge_trashed_works(data: PurgeRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    commit = await svc.purge(data.work_ids, message=data.message)
    await project_commit_safe(db, commit.id)
    return await svc.commit_payload(commit.id)


@router.get("/rule-suggestions", response_model=list[RuleSuggestionRead])
async def curation_rule_suggestions(db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    return await svc.rule_suggestions()


@router.get("/gitllery/status", response_model=GitlleryStatusResponse)
async def gitllery_status(deep: bool = Query(False), db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).status(deep=deep)


@router.get("/repositories/{repository_id}/gitllery/status", response_model=GitlleryStatusResponse)
async def gitllery_repo_status(repository_id: str, deep: bool = Query(False),
                               db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).status(repository_id, deep=deep)


@router.post("/gitllery/reconcile")
async def gitllery_reconcile(repository_id: str | None = None):
    """Enqueue projection of pending commits (and checkpoint rebuild when
    library-wide). The walk is O(history) and runs in a worker by design —
    status() has no inline full-history fallback anymore."""
    from app.services.operations import enqueue_admin_operation
    return await enqueue_admin_operation(
        lock_key="library:gitllery-sync:active",
        operation_type="admin-gitllery-sync",
        title="Gitllery sync",
        entity="gitllery-sync",
        func="app.jobs.admin_operations.run_gitllery_sync_operation",
        options={"mode": "reconcile", "repository_id": repository_id},
    )


@router.post("/gitllery/backfill")
async def gitllery_backfill():
    """Enqueue repo initialization + full projection (first-sync path)."""
    from app.services.operations import enqueue_admin_operation
    return await enqueue_admin_operation(
        lock_key="library:gitllery-sync:active",
        operation_type="admin-gitllery-sync",
        title="Gitllery sync",
        entity="gitllery-sync",
        func="app.jobs.admin_operations.run_gitllery_sync_operation",
        options={"mode": "backfill", "repository_id": None},
    )


@router.get("/repositories/{repository_id}/gitllery/log", response_model=GitlleryLogResponse)
async def gitllery_log(repository_id: str, limit: int = Query(50, ge=1, le=200),
                       db: AsyncSession = Depends(get_db)):
    return await GitlleryService(db).log(repository_id, limit)


@router.post("/gitllery/rebuild", response_model=GitlleryRebuildResponse)
async def gitllery_rebuild(
    dry_run: bool = Query(True),
    repository_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Restore manual curation history from on-disk .gitllery repos into the DB.

    dry_run (default) runs inline and reports what WOULD happen without writing.
    dry_run=false enqueues the real rebuild as an operations job (mirrors
    import_from_disk in admin.py) since it may touch a large history.
    """
    if dry_run:
        report = await GitlleryService(db).rebuild_db_from_disk(repository_id, dry_run=True)
        return report

    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import get_operation_status, set_operation_status
    from app.services.tasks import TaskService

    options = {"repository_id": repository_id}
    redis = get_redis()
    job_id = str(uuid.uuid4())
    active_job = redis.get("library:gitllery-rebuild:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:gitllery-rebuild:active")
    if not redis.set("library:gitllery-rebuild:active", job_id, nx=True, ex=604800):
        active_job = redis.get("library:gitllery-rebuild:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Gitllery rebuild already running", "job_id": active_job})

    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-gitllery-rebuild",
            title="Rebuild curation from disk",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Gitllery rebuild queued"},
            meta={"entity": "gitllery-rebuild", "repository_id": repository_id},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-gitllery-rebuild",
        progress={"phase": "enqueued", "label": "Gitllery rebuild queued"},
        meta={"entity": "gitllery-rebuild", "repository_id": repository_id})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_gitllery_rebuild_operation",
        job_id, options, job_timeout=14400, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "dry_run": False}
