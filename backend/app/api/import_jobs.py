from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.import_job import ImportJob

router = APIRouter(dependencies=[RequireAdmin])


@router.post("/scan")
async def scan_imports():
    return {"status": "ok", "message": "Import job scan triggered", "jobs_found": 0}


@router.get("")
async def list_import_jobs(status: str | None = None, offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    stmt = select(ImportJob).offset(offset).limit(limit).order_by(ImportJob.created_at.desc())
    if status:
        stmt = stmt.where(ImportJob.status == status)
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())
    return [{"id": str(j.id), "download_job_id": str(j.download_job_id), "status": j.status, "error_log": j.error_log, "created_at": j.created_at.isoformat(), "updated_at": j.updated_at.isoformat()} for j in jobs]


@router.get("/{job_id}")
async def get_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImportJob).where(ImportJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return {"id": str(job.id), "download_job_id": str(job.download_job_id), "status": job.status, "error_log": job.error_log, "created_at": job.created_at.isoformat(), "updated_at": job.updated_at.isoformat()}


@router.post("/{job_id}/retry")
async def retry_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImportJob).where(ImportJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    if job.status not in ("failed", "stale"):
        raise HTTPException(status_code=400, detail=f"Cannot retry job with status '{job.status}'")
    job.status = "pending"
    job.error_log = None
    await db.commit()

    try:
        import redis as redis_lib
        from rq import Queue
        from app.config import settings
        r = redis_lib.from_url(settings.redis_url)
        Queue(connection=r).enqueue("app.jobs.import_runner.run_import_job", str(job_id))
    except Exception:
        pass

    return {"status": "ok", "message": f"Retry triggered for import job {job_id}"}


@router.delete("/{job_id}")
async def delete_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImportJob).where(ImportJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    if job.status in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot delete job with status '{job.status}'")
    await db.delete(job)
    await db.commit()
    return {"status": "ok"}
