from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from app.auth import RequirePermission
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.download_job import DownloadJobCreate, DownloadJobRead
from app.schemas.import_job import ImportJobRead
from app.services.download import DownloadService
from app.services.progress import ProgressTracker
from app.services.search_language import SearchQueryError
from app.services.task_engine import TaskEngine, TaskEngineError
from app.services.backpressure import DownloadAdmissionError

router = APIRouter(dependencies=[RequirePermission("tasks")])


@router.get("", response_model=list[DownloadJobRead])
async def list_jobs(status: str | None = None, source: str | None = None,
                    subscription_id: str | None = None,
                    subscription_source_id: str | None = None,
                    q: str | None = None,
                    visibility: Literal["actionable", "all"] = "all",
                    sort_by: str = "created_at", sort_order: str = "desc",
                    offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db),
                    user=RequirePermission("tasks")):
    svc = DownloadService(db)
    try:
        return await svc.list_jobs(status=status, source=source,
                                   subscription_id=subscription_id,
                                   subscription_source_id=subscription_source_id,
                                   q=q,
                                   visibility=visibility,
                                   sort_by=sort_by, sort_order=sort_order,
                                   offset=offset, limit=limit, user_id=user.id)
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc


@router.get("/{job_id}", response_model=DownloadJobRead)
async def get_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    svc = DownloadService(db)
    try:
        return await svc.get_job(job_id, user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", status_code=201)
async def create_job(
    data: DownloadJobCreate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    svc = DownloadService(db)
    try:
        from app.services.subscription_membership import SubscriptionMembershipService

        membership_service = SubscriptionMembershipService(db, user.id)
        member = await membership_service.require_membership(data.subscription_id)
        remote_account_id = None
        if data.subscription_source_id is not None:
            owned_sources = await membership_service.list_sources(data.subscription_id)
            owned_source = next(
                (source for source in owned_sources if source.id == data.subscription_source_id),
                None,
            )
            if owned_source is None:
                raise ValueError("Subscription source not found")
            remote_account_id = owned_source.remote_account_id
        payload = data.model_dump()
        payload["triggering_user_subscription_id"] = member.id
        payload["triggering_remote_account_id"] = remote_account_id
        return await svc.create_job(payload, user_id=user.id)
    except DownloadAdmissionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.payload()) from e
    except ValueError as e:
        status = 404 if "not found" in str(e).casefold() else 400
        raise HTTPException(status_code=status, detail=str(e)) from e


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    engine = TaskEngine(db)
    try:
        await DownloadService(db).get_job(job_id, user_id=user.id)
        result = await engine.retry_download(job_id, operator=user.username)
        await db.commit()
        return result
    except DownloadAdmissionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.payload()) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from e
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{job_id}")
async def delete_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    engine = TaskEngine(db)
    try:
        await DownloadService(db).get_job(job_id, user_id=user.id)
        await engine.delete_download(job_id)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from e
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/pause")
async def pause_job(
    job_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Pause a download job. Sends SIGTERM to the running process group."""
    engine = TaskEngine(db)
    try:
        note = data.get("note") if data else None
        await DownloadService(db).get_job(job_id, user_id=user.id)
        result = await engine.pause_download(job_id, note=note, operator=user.username)
        await db.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from e
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Resume a paused download job by re-enqueuing it."""
    engine = TaskEngine(db)
    try:
        await DownloadService(db).get_job(job_id, user_id=user.id)
        result = await engine.resume_download(job_id, operator=user.username)
        await db.commit()
        return result
    except DownloadAdmissionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.payload()) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from e
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/batch")
async def batch_jobs(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Batch action on multiple download jobs via TaskEngine.

    Accepts: {ids: [UUID, ...], action: "retry"|"delete"|"pause"|"resume"|"cancel"}
    """
    ids_raw = data.get("ids", [])
    action = data.get("action", "")
    note = data.get("note")
    if not ids_raw or not isinstance(ids_raw, list):
        raise HTTPException(status_code=400, detail="ids list is required")
    if action not in ("retry", "delete", "pause", "resume", "cancel"):
        raise HTTPException(status_code=400, detail="action must be retry/delete/pause/resume/cancel")
    ids = [UUID(i) for i in ids_raw]
    engine = TaskEngine(db)
    for job_id in ids:
        try:
            await DownloadService(db).get_job(job_id, user_id=user.id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="DownloadJob not found") from exc
    # Reuse batch_by_filter with an explicit ID list
    result = await engine.batch_by_filter(
        "download", {"ids": [str(i) for i in ids]}, action,
        operator=user.username, note=note,
    )
    await db.commit()
    return result


@router.post("/clear")
async def clear_jobs(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Delete all jobs matching given statuses."""
    statuses = data.get("statuses", [])
    if not statuses or not isinstance(statuses, list):
        raise HTTPException(status_code=400, detail="statuses list is required")
    svc = DownloadService(db)
    count = await svc.clear_completed(statuses, user_id=user.id)
    return {"status": "ok", "deleted": count}


@router.post("/kill-stuck")
async def kill_stuck(
    db: AsyncSession = Depends(get_db),
    system_user=RequirePermission("system"),
):
    """Detect and mark stale tasks via heartbeat timeout."""
    engine = TaskEngine(db)
    count = await engine.detect_stale_tasks()
    await db.commit()
    return {"status": "ok", "killed": count}


@router.post("/retry-all")
async def retry_all_failed(
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Retry all failed and stale download jobs via batch-by-filter."""
    engine = TaskEngine(db)
    failed = await DownloadService(db).list_jobs(status="failed", limit=10000, user_id=user.id)
    stale = await DownloadService(db).list_jobs(status="stale", limit=10000, user_id=user.id)
    result = await engine.batch_by_filter(
        "download", {"ids": [str(job.id) for job in failed]}, "retry", operator=user.username)
    stale_result = await engine.batch_by_filter(
        "download", {"ids": [str(job.id) for job in stale]}, "retry", operator=user.username)
    total = {
        "succeeded": result["succeeded"] + stale_result["succeeded"],
        "failed": result["failed"] + stale_result["failed"],
        "errors": result.get("errors", []) + stale_result.get("errors", []),
    }
    await db.commit()
    return {"status": "ok", **total}


@router.get("/{job_id}/imports", response_model=list[ImportJobRead])
async def list_imports(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    svc = DownloadService(db)
    try:
        await svc.get_job(job_id, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from exc
    return await svc.list_imports(job_id)


# ── Task Engine endpoints (Phase 7) ──────────────────────────────


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Cancel a download job. Sends SIGTERM to the process group. Terminal."""
    engine = TaskEngine(db)
    try:
        note = data.get("note") if data else None
        await DownloadService(db).get_job(job_id, user_id=user.id)
        result = await engine.cancel_download(job_id, note=note, operator=user.username)
        await db.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from e
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/priority")
async def set_priority(
    job_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Set the priority of a download job."""
    priority = data.get("priority", 10)
    engine = TaskEngine(db)
    try:
        await DownloadService(db).get_job(job_id, user_id=user.id)
        result = await engine.set_priority_download(job_id, priority, operator=user.username)
        await db.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail="DownloadJob not found") from e
    except TaskEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/batch-by-filter")
async def batch_by_filter(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Batch action on download jobs matching filter criteria.

    Accepts: ``{"filters": {"source": "pixiv", "status": "failed"}, "action": "retry", "note": "..."}``
    """
    filters = data.get("filters", {})
    action = data.get("action", "")
    note = data.get("note")
    engine = TaskEngine(db)
    try:
        jobs = await DownloadService(db).list_jobs(
            status=filters.get("status"),
            source=filters.get("source"),
            subscription_id=filters.get("subscription_id"),
            subscription_source_id=filters.get("subscription_source_id"),
            limit=10000,
            user_id=user.id,
        )
        result = await engine.batch_by_filter(
            "download",
            {"ids": [str(job.id) for job in jobs]},
            action,
            operator=user.username,
            note=note,
        )
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{job_id}/progress")
async def get_progress(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Get the latest progress snapshot for a download job."""
    try:
        job = await DownloadService(db).get_job(job_id, user_id=user.id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found")
    progress = job.progress_data
    if not progress:
        raise HTTPException(status_code=404, detail="No progress data available")
    return {"job_id": str(job_id), **progress}


@router.get("/{job_id}/pipeline")
async def get_pipeline(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("tasks"),
):
    """Get pipeline stage data for a download job."""
    svc = DownloadService(db)
    try:
        job = await svc.get_job(job_id, user_id=user.id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found")

    from app.models.task_state import PipelineStage
    stages = PipelineStage.ordered_stages()
    current_stage = getattr(job, "pipeline_stage", None)

    pipeline = []
    for s in stages:
        status = "pending"
        if s == current_stage:
            status = "active"
        elif stages.index(s) < stages.index(current_stage or stages[0]):
            status = "complete"
        pipeline.append({"name": s, "status": status})

    return {
        "job_id": str(job.id),
        "current_stage": current_stage,
        "stages": pipeline,
        "progress": job.progress_data,
    }
