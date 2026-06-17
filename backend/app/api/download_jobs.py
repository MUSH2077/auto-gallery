from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from app.auth import RequireAdmin, get_admin_key
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.download_job import DownloadJobCreate, DownloadJobRead
from app.schemas.import_job import ImportJobRead
from app.services.download import DownloadService
from app.services.progress import ProgressTracker
from app.services.task_engine import TaskEngine, TaskEngineError

router = APIRouter(dependencies=[RequireAdmin])


@router.get("", response_model=list[DownloadJobRead])
async def list_jobs(status: str | None = None, source: str | None = None,
                    subscription_id: str | None = None,
                    subscription_source_id: str | None = None,
                    q: str | None = None,
                    sort_by: str = "created_at", sort_order: str = "desc",
                    offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    return await svc.list_jobs(status=status, source=source,
                               subscription_id=subscription_id,
                               subscription_source_id=subscription_source_id,
                               q=q,
                               sort_by=sort_by, sort_order=sort_order,
                               offset=offset, limit=limit)


@router.get("/{job_id}", response_model=DownloadJobRead)
async def get_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    try:
        return await svc.get_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", status_code=201)
async def create_job(data: DownloadJobCreate, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    try:
        return await svc.create_job(data.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/retry")
async def retry_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    try:
        return await svc.retry_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{job_id}")
async def delete_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    try:
        await svc.delete_job(job_id)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{job_id}/pause")
async def pause_job(
    job_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    """Pause a download job. Sends SIGTERM to the running process group."""
    engine = TaskEngine(db)
    try:
        note = data.get("note") if data else None
        result = await engine.pause_download(job_id, note=note, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    """Resume a paused download job by re-enqueuing it."""
    engine = TaskEngine(db)
    try:
        result = await engine.resume_download(job_id, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/batch")
async def batch_jobs(
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
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
    # Reuse batch_by_filter with an explicit ID list
    result = await engine.batch_by_filter(
        "download", {"ids": [str(i) for i in ids]}, action,
        operator=operator, note=note,
    )
    await db.commit()
    return result


@router.post("/clear")
async def clear_jobs(data: dict, db: AsyncSession = Depends(get_db)):
    """Delete all jobs matching given statuses."""
    statuses = data.get("statuses", [])
    if not statuses or not isinstance(statuses, list):
        raise HTTPException(status_code=400, detail="statuses list is required")
    svc = DownloadService(db)
    count = await svc.clear_completed(statuses)
    return {"status": "ok", "deleted": count}


@router.post("/kill-stuck")
async def kill_stuck(db: AsyncSession = Depends(get_db)):
    """Mark all 'downloading' jobs as stale."""
    svc = DownloadService(db)
    count = await svc.kill_stuck_jobs()
    return {"status": "ok", "killed": count}


@router.post("/retry-all")
async def retry_all_failed(db: AsyncSession = Depends(get_db)):
    """Retry all failed and stale download jobs."""
    svc = DownloadService(db)
    jobs = await svc.list_jobs(status="failed")
    jobs += await svc.list_jobs(status="stale")
    results = {"succeeded": 0, "failed": 0, "errors": []}
    for j in jobs:
        try:
            await svc.retry_job(j.id)
            results["succeeded"] += 1
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"id": str(j.id), "error": str(e)})
    return {"status": "ok", **results}


@router.get("/{job_id}/imports", response_model=list[ImportJobRead])
async def list_imports(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    return await svc.list_imports(job_id)


# ── Task Engine endpoints (Phase 7) ──────────────────────────────


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    """Cancel a download job. Sends SIGTERM to the process group. Terminal."""
    engine = TaskEngine(db)
    try:
        note = data.get("note") if data else None
        result = await engine.cancel_download(job_id, note=note, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/priority")
async def set_priority(
    job_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    """Set the priority of a download job."""
    priority = data.get("priority", 10)
    engine = TaskEngine(db)
    try:
        result = await engine.set_priority_download(job_id, priority, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/batch-by-filter")
async def batch_by_filter(
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    """Batch action on download jobs matching filter criteria.

    Accepts: ``{"filters": {"source": "pixiv", "status": "failed"}, "action": "retry", "note": "..."}``
    """
    filters = data.get("filters", {})
    action = data.get("action", "")
    note = data.get("note")
    engine = TaskEngine(db)
    try:
        result = await engine.batch_by_filter("download", filters, action, operator=operator, note=note)
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{job_id}/progress")
async def get_progress(job_id: UUID):
    """Get the latest progress snapshot for a download job."""
    progress = ProgressTracker.get(str(job_id))
    if not progress:
        raise HTTPException(status_code=404, detail="No progress data available")
    return {"job_id": str(job_id), **progress}


@router.get("/{job_id}/pipeline")
async def get_pipeline(job_id: UUID, db: AsyncSession = Depends(get_db)):
    """Get pipeline stage data for a download job."""
    svc = DownloadService(db)
    try:
        job = await svc.get_job(job_id)
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
        "progress": ProgressTracker.get(str(job_id)),
    }
