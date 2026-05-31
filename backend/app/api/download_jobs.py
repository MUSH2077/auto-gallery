from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.download_job import DownloadJobCreate, DownloadJobRead
from app.schemas.import_job import ImportJobRead
from app.services.download import DownloadService

router = APIRouter(dependencies=[RequireAdmin])


@router.get("", response_model=list[DownloadJobRead])
async def list_jobs(status: str | None = None, source: str | None = None,
                    subscription_id: str | None = None,
                    sort_by: str = "created_at", sort_order: str = "desc",
                    offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    return await svc.list_jobs(status=status, source=source,
                               subscription_id=subscription_id,
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
async def pause_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    try:
        return await svc.pause_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/resume")
async def resume_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    try:
        return await svc.resume_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/batch")
async def batch_jobs(data: dict, db: AsyncSession = Depends(get_db)):
    """Batch action on multiple download jobs.

    Accepts: {ids: [UUID, ...], action: "retry"|"delete"|"pause"|"resume"}
    """
    ids_raw = data.get("ids", [])
    action = data.get("action", "")
    if not ids_raw or not isinstance(ids_raw, list):
        raise HTTPException(status_code=400, detail="ids list is required")
    if action not in ("retry", "delete", "pause", "resume"):
        raise HTTPException(status_code=400, detail="action must be retry/delete/pause/resume")
    ids = [UUID(i) for i in ids_raw]
    svc = DownloadService(db)
    return await svc.batch_action(ids, action)


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
