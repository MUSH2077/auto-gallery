from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequirePermission
from sqlalchemy import delete as sql_delete, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.tag import TagRead, TagCreate, TagUpdate, TagDetail, CreatorRef
from app.repositories.tag import TagRepository
from app.services.search_projection_outbox import request_search_projection
from app.models.tag import Tag
from app.models.work_tag import WorkTag
from app.models.work_source import WorkSource
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
    await request_search_projection(db, tag_ids=[tag.id])
    await db.commit()
    return tag


@curation_router.put("/{tag_id}", response_model=TagRead)
async def update_tag(tag_id: UUID, data: TagUpdate, db: AsyncSession = Depends(get_db)):
    repo = TagRepository(db)
    tag = await repo.get(tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    affected_work_ids = set((await db.execute(
        select(WorkTag.work_id).where(WorkTag.tag_id == tag_id)
    )).scalars().all())
    affected_work_ids.update((await db.execute(
        select(WorkSource.work_id)
        .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
        .where(WorkSourceTag.tag_id == tag_id)
    )).scalars().all())
    tag = await repo.update(tag, data.model_dump(exclude_unset=True))
    await request_search_projection(
        db,
        affected_work_ids,
        tag_ids=[tag.id],
    )
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

    # Capture affected works once, then merge both relation tables with four
    # set-based statements.  The previous ORM loop issued up to two queries per
    # one of ~470k associations.
    affected_work_ids = set((await db.execute(
        select(WorkTag.work_id).where(WorkTag.tag_id == source_uuid)
    )).scalars().all())
    affected_work_ids.update((await db.execute(
        select(WorkSource.work_id)
        .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
        .where(WorkSourceTag.tag_id == source_uuid)
    )).scalars().all())

    duplicate_direct_ids = select(WorkTag.id).where(
        WorkTag.tag_id == source_uuid,
        WorkTag.work_id.in_(
            select(WorkTag.work_id).where(WorkTag.tag_id == target_uuid)
        ),
    )
    await db.execute(sql_delete(WorkTag).where(WorkTag.id.in_(duplicate_direct_ids)))
    await db.execute(
        sql_update(WorkTag)
        .where(WorkTag.tag_id == source_uuid)
        .values(tag_id=target_uuid)
    )

    duplicate_source_ids = select(WorkSourceTag.id).where(
        WorkSourceTag.tag_id == source_uuid,
        WorkSourceTag.work_source_id.in_(
            select(WorkSourceTag.work_source_id).where(
                WorkSourceTag.tag_id == target_uuid
            )
        ),
    )
    await db.execute(
        sql_delete(WorkSourceTag).where(WorkSourceTag.id.in_(duplicate_source_ids))
    )
    await db.execute(
        sql_update(WorkSourceTag)
        .where(WorkSourceTag.tag_id == source_uuid)
        .values(tag_id=target_uuid)
    )

    # Delete source tag
    source_tag = await db.get(Tag, source_uuid)
    if source_tag:
        await db.delete(source_tag)

    await request_search_projection(
        db,
        affected_work_ids,
        tag_ids=[target_uuid],
        deleted_tag_ids=[source_uuid],
    )
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
    direct_work_ids = set((await db.execute(
        select(WorkTag.work_id).where(WorkTag.tag_id == tag_id)
    )).scalars().all())
    source_work_ids = set((await db.execute(
        select(WorkSource.work_id)
        .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
        .where(WorkSourceTag.tag_id == tag_id)
    )).scalars().all())
    await repo.delete(tag)
    await request_search_projection(
        db,
        direct_work_ids | source_work_ids,
        deleted_tag_ids=[tag_id],
    )
    await db.commit()
    return {"status": "ok"}
