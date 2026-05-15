from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.import_job import ImportJob

router = APIRouter()


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
async def retry_import_job(job_id: UUID):
    return {"status": "ok", "message": f"Retry triggered for import job {job_id}"}
