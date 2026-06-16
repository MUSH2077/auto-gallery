from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.tag import TagRead, TagCreate, TagUpdate
from app.repositories.tag import TagRepository
from app.models.tag import Tag
from app.models.work_tag import WorkTag
from app.models.work_source_tag import WorkSourceTag

router = APIRouter(dependencies=[RequireAdmin])


@router.get("", response_model=list[TagRead])
async def list_tags(offset: int = 0, limit: int = 100,
                    sort_by: str = "usage_count",
                    sort_order: str = "desc",
                    db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.list_all(offset, limit, sort_by=sort_by, sort_order=sort_order)


@router.get("/{tag_id}", response_model=TagRead)
async def get_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    return tag


@router.post("", response_model=TagRead)
async def create_tag(data: TagCreate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.create(data.model_dump())
    await db.commit()
    return tag


@router.put("/{tag_id}", response_model=TagRead)
async def update_tag(tag_id: UUID, data: TagUpdate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag = await repo.update(tag, data.model_dump(exclude_unset=True))
    await db.commit()
    return tag




@router.post("/merge")
async def merge_tags(data: dict, db: AsyncSession = Depends(get_db)):
    """Merge source tag into target tag. All work_tag and work_source_tag references
    are moved to the target, and the source tag is deleted."""
    source_id = data.get("source_id")
    target_id = data.get("target_id")
    if not source_id or not target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id required")

    from app.models.work_tag import WorkTag
    from app.models.work_source_tag import WorkSourceTag

    source_uuid = UUID(source_id)
    target_uuid = UUID(target_id)

    # Move work_tags
    wts = await db.execute(select(WorkTag).where(WorkTag.tag_id == source_uuid))
    for wt in wts.scalars().all():
        exists = await db.execute(
            select(WorkTag).where(WorkTag.work_id == wt.work_id, WorkTag.tag_id == target_uuid)
        )
        if not exists.scalar_one_or_none():
            wt.tag_id = target_uuid
        else:
            await db.delete(wt)

    # Move work_source_tags
    wsts = await db.execute(select(WorkSourceTag).where(WorkSourceTag.tag_id == source_uuid))
    for wst in wsts.scalars().all():
        exists = await db.execute(
            select(WorkSourceTag).where(WorkSourceTag.work_source_id == wst.work_source_id, WorkSourceTag.tag_id == target_uuid)
        )
        if not exists.scalar_one_or_none():
            wst.tag_id = target_uuid
        else:
            await db.delete(wst)

    # Delete source tag
    source_tag = await db.get(Tag, source_uuid)
    if source_tag:
        await db.delete(source_tag)

    await db.commit()
    return {"status": "ok", "message": "Tags merged"}


@router.post("/{tag_id}/alias")
async def set_tag_alias(tag_id: UUID, data: dict, db: AsyncSession = Depends(get_db)):
    """Set an alias for a tag. The alias becomes a new tag that redirects to this one."""
    pass  # alias system requires a model change — defer to future migration


@router.delete("/{tag_id}")
async def delete_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    await repo.delete(tag)
    await db.commit()
    return {"status": "ok"}
