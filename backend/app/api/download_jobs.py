from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.download_job import DownloadJobCreate, DownloadJobRead
from app.schemas.import_job import ImportJobRead
from app.services.download import DownloadService

router = APIRouter()


@router.get("", response_model=list[DownloadJobRead])
async def list_jobs(status: str | None = None, offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    return await svc.list_jobs(status, offset, limit)


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


@router.get("/{job_id}/imports", response_model=list[ImportJobRead])
async def list_imports(job_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = DownloadService(db)
    return await svc.list_imports(job_id)
