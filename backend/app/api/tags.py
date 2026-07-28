from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequirePermission
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.tag import TagRead, TagCreate, TagUpdate, TagDetail, CreatorRef
from app.repositories.tag import TagRepository
from app.models.tag import Tag
from app.models.work_tag import WorkTag
from app.models.work_source_tag import WorkSourceTag

router = APIRouter(dependencies=[RequirePermission("library")])
# Write/mutation routes on the `tag` library entity belong to the
# `curation` module per spec §A2 (create/update/delete/merge/alias are
# tag-editing, a curation operation, not library browsing). Included with
# the same "/tags" prefix in app/api/__init__.py, so URLs are unchanged.
curation_router = APIRouter(dependencies=[RequirePermission("curation")])


@router.get("", response_model=list[TagRead])
async def list_tags(offset: int = 0, limit: int = 100,
                    sort_by: str = "usage_count",
                    sort_order: str = "desc",
                    db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    return await repo.list_all(offset, limit, sort_by=sort_by, sort_order=sort_order)


@router.get("/{tag_id}", response_model=TagDetail)
async def get_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    from app.models.work_tag import WorkTag
    from app.models.work_source import WorkSource
    from app.models.source_creator import SourceCreator
    from app.models.creator import Creator
    from sqlalchemy import func

    usage_count_result = await db.execute(
        select(func.count(WorkTag.work_id)).where(WorkTag.tag_id == tag_id)
    )
    usage_count = usage_count_result.scalar() or 0

    top_creators_rows = await db.execute(
        select(
            Creator.id,
            func.coalesce(Creator.display_name, Creator.name).label("creator_name"),
            func.count(func.distinct(WorkTag.work_id)).label("work_count"),
        )
        .join(SourceCreator, SourceCreator.creator_id == Creator.id)
        .join(WorkSource, WorkSource.source_creator_id == SourceCreator.source_creator_id)
        .join(WorkTag, WorkTag.work_id == WorkSource.work_id)
        .where(WorkTag.tag_id == tag_id)
        .group_by(Creator.id)
        .order_by(func.count(func.distinct(WorkTag.work_id)).desc())
        .limit(10)
    )
    top_creators = [
        CreatorRef(creator_id=r[0], creator_name=str(r[1]), work_count=r[2])
        for r in top_creators_rows.all()
    ]

    return TagDetail(
        id=tag.id,
        normalized_name=tag.normalized_name,
        category=tag.category,
        usage_count=usage_count,
        created_at=tag.created_at,
        top_creators=top_creators,
    )


@curation_router.post("", response_model=TagRead)
async def create_tag(data: TagCreate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.create(data.model_dump())
    await db.commit()
    return tag


@curation_router.put("/{tag_id}", response_model=TagRead)
async def update_tag(tag_id: UUID, data: TagUpdate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag = await repo.update(tag, data.model_dump(exclude_unset=True))
    await db.commit()
    return tag




@curation_router.post("/merge")
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


@curation_router.post("/{tag_id}/alias")
async def set_tag_alias(tag_id: UUID, data: dict, db: AsyncSession = Depends(get_db)):
    """Set an alias for a tag. The alias becomes a new tag that redirects to this one."""
    pass  # alias system requires a model change — defer to future migration


@curation_router.delete("/{tag_id}")
async def delete_tag(tag_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    await repo.delete(tag)
    await db.commit()
    return {"status": "ok"}
