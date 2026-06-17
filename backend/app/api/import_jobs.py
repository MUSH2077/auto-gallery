import logging
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin, get_admin_key
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, String, or_

from app.database import get_db
from app.models.import_job import ImportJob
from app.schemas.import_job import ImportJobRead
from app.services.job_state import transition_import_job
from app.services.progress import ProgressTracker
from app.services.task_engine import TaskEngine, TaskEngineError

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[RequireAdmin])


@router.post("/scan")
async def scan_imports(db: AsyncSession = Depends(get_db)):
    """Scan for pending import work: check downloads dir for unprocessed JSON metadata files."""
    import os
    from pathlib import Path
    from app.config import settings

    job_count = 0
    # Find all download dirs that have metadata JSONs from --write-metadata
    dl_root = Path(settings.download_root)
    if dl_root.exists():
        for source_dir in dl_root.iterdir():
            if not source_dir.is_dir():
                continue
            for user_dir in source_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                for work_dir in user_dir.iterdir():
                    if not work_dir.is_dir():
                        continue
                    json_files = list(work_dir.glob("*.json"))
                    if json_files:
                        job_count += 1

    # Also count existing failed import jobs that can be retried
    failed = await db.execute(
        select(ImportJob).where(ImportJob.status.in_(["failed", "stale"]))
    )
    failed_count = len(failed.scalars().all())

    return {
        "status": "ok",
        "message": f"Found {job_count} directories with pending metadata, {failed_count} retryable jobs",
        "pending_dirs": job_count,
        "retryable_jobs": failed_count,
    }


@router.get("")
async def list_import_jobs(
    status: str | None = None,
    download_job_id: UUID | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ImportJob)
    if status:
        stmt = stmt.where(ImportJob.status == status)
    if download_job_id:
        stmt = stmt.where(ImportJob.download_job_id == download_job_id)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            cast(ImportJob.id, String).ilike(pattern),
            cast(ImportJob.download_job_id, String).ilike(pattern),
            ImportJob.error_log.ilike(pattern),
        ))
    count_stmt = select(func.count(ImportJob.id))
    if status:
        count_stmt = count_stmt.where(ImportJob.status == status)
    if download_job_id:
        count_stmt = count_stmt.where(ImportJob.download_job_id == download_job_id)
    if q:
        pattern = f"%{q.strip()}%"
        count_stmt = count_stmt.where(or_(
            cast(ImportJob.id, String).ilike(pattern),
            cast(ImportJob.download_job_id, String).ilike(pattern),
            ImportJob.error_log.ilike(pattern),
        ))
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()
    stmt = stmt.order_by(ImportJob.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())
    return {"total": total, "items": [ImportJobRead.model_validate(j).model_dump(mode="json") for j in jobs]}


@router.get("/{job_id}")
async def get_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImportJob).where(ImportJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return ImportJobRead.model_validate(job).model_dump(mode="json")


@router.post("/{job_id}/retry")
async def retry_import_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    engine = TaskEngine(db)
    try:
        result = await engine.retry_import(job_id, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{job_id}")
async def delete_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    engine = TaskEngine(db)
    try:
        await engine.delete_import(job_id)
        return {"status": "ok"}
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ── Task Engine endpoints ─────────────────────────────────────────


@router.post("/{job_id}/cancel")
async def cancel_import_job(
    job_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    engine = TaskEngine(db)
    try:
        note = data.get("note") if data else None
        result = await engine.cancel_import(job_id, note=note, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/priority")
async def set_import_priority(
    job_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    priority = data.get("priority", 10)
    engine = TaskEngine(db)
    try:
        result = await engine.set_priority_import(job_id, priority, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/batch-by-filter")
async def batch_import_by_filter(
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    filters = data.get("filters", {})
    action = data.get("action", "")
    note = data.get("note")
    engine = TaskEngine(db)
    try:
        return await engine.batch_by_filter("import", filters, action, operator=operator, note=note)
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{job_id}/progress")
async def get_import_progress(job_id: UUID):
    progress = ProgressTracker.get(str(job_id))
    if not progress:
        raise HTTPException(status_code=404, detail="No progress data available")
    return {"job_id": str(job_id), **progress}

