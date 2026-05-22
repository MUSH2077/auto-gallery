from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.work import WorkRead, WorkList
from app.repositories.work import WorkRepository
from app.models.asset import Asset
from app.models.asset_source import AssetSource
from app.models.work_source import WorkSource
from app.models.tag import Tag
from app.models.work_tag import WorkTag

router = APIRouter()


@router.get("", response_model=list[WorkList])
async def list_works(
    offset: int = 0, limit: int = 50,
    search: str | None = None,
    source: str | None = None,
    creator_id: str | None = None,
    is_nsfw: bool | None = None,
    is_favorite: bool | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    db: AsyncSession = Depends(get_db),
):
    repo = WorkRepository(db)
    return await repo.list_all(
        offset=offset, limit=limit,
        search=search, source=source, creator_id=creator_id,
        is_nsfw=is_nsfw, is_favorite=is_favorite,
        sort_by=sort_by, sort_order=sort_order,
    )


@router.get("/{work_id}", response_model=WorkRead)
async def get_work(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    return work


@router.post("/{work_id}/favorite", response_model=WorkRead)
async def toggle_work_favorite(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    work = await repo.toggle_favorite(work)
    await db.refresh(work)
    return work


@router.get("/{work_id}/sources")
async def get_work_sources(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    return await repo.get_sources(work_id)


@router.get("/{work_id}/tags")
async def get_work_tags(work_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Tag).join(WorkTag, WorkTag.tag_id == Tag.id)
        .where(WorkTag.work_id == work_id)
    )
    tags = result.scalars().all()
    return [{"id": str(t.id), "normalized_name": t.normalized_name, "category": t.category} for t in tags]


@router.get("/{work_id}/assets")
async def get_work_assets(work_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Asset).join(AssetSource, AssetSource.asset_id == Asset.id)
        .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
        .where(WorkSource.work_id == work_id)
        .order_by(Asset.created_at)
    )
    assets = result.scalars().all()
    return [{
        "id": str(a.id), "file_name": a.file_name, "file_path": a.file_path,
        "width": a.width, "height": a.height, "mime_type": a.mime_type,
        "thumb_sm_path": a.thumb_sm_path, "thumb_md_path": a.thumb_md_path,
        "created_at": a.created_at.isoformat(),
    } for a in assets]
