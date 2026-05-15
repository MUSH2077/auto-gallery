from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.work import WorkRead, WorkList
from app.repositories.work import WorkRepository

router = APIRouter()


@router.get("", response_model=list[WorkList])
async def list_works(offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    return await repo.list_all(offset, limit)


@router.get("/{work_id}", response_model=WorkRead)
async def get_work(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    return work


@router.get("/{work_id}/sources")
async def get_work_sources(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    return await repo.get_sources(work_id)
