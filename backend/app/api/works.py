from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequirePermission
from app.models.work import Work
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.work import WorkRead, WorkList, WorkListResponse
from app.schemas.curation import BatchCurateRequest, CurationCommitRead
from app.repositories.work import WorkRepository
from app.services.cache import cache_get, cache_set, cache_key, cache_delete_pattern
from app.models.asset import Asset
from app.models.asset_source import AssetSource
from app.models.work import Work
from app.models.work_source import WorkSource
from app.models.tag import Tag
from app.models.work_tag import WorkTag
from app.services.media_signing import signed_media_url
from app.services.curation import CurationService
from app.services.gitllery import project_commit_safe

router = APIRouter(dependencies=[RequirePermission("library")])


@router.get("", response_model=WorkListResponse)
async def list_works(
    offset: int = 0, limit: int = 50,
    search: str | None = None,
    source: str | None = None,
    creator_id: str | None = None,
    tag: str | None = None,
    is_nsfw: bool | None = None,
    is_favorite: bool | None = None,
    is_ai_generated: bool | None = None,
    curation_visibility: str = "visible",
    sort_by: str = "created_at",
    sort_order: str = "desc",
    db: AsyncSession = Depends(get_db),
):
    ck = cache_key("works:list", offset=offset, limit=limit,
                   search=search, source=source, creator_id=creator_id, tag=tag,
        is_nsfw=is_nsfw, is_favorite=is_favorite,
        is_ai_generated=is_ai_generated,
        curation_visibility=curation_visibility,
        sort_by=sort_by, sort_order=sort_order)
    cached = cache_get(ck)
    if cached is not None:
        return cached
    # The total count is a full scan of the filtered set and is identical across
    # pages of the same filter — cache it separately (filters only, no offset/
    # sort) so deep paging doesn't re-run the COUNT for every page.
    count_ck = cache_key("works:count",
                         search=search, source=source, creator_id=creator_id, tag=tag,
                         is_nsfw=is_nsfw, is_favorite=is_favorite,
                         is_ai_generated=is_ai_generated,
                         curation_visibility=curation_visibility)
    cached_total = cache_get(count_ck)
    repo = WorkRepository(db)
    works, total = await repo.list_all(
        offset=offset, limit=limit,
        search=search, source=source, creator_id=creator_id, tag=tag,
        is_nsfw=is_nsfw, is_favorite=is_favorite,
        is_ai_generated=is_ai_generated,
        curation_visibility=curation_visibility,
        sort_by=sort_by, sort_order=sort_order,
        precomputed_total=cached_total,
    )
    if cached_total is None:
        cache_set(count_ck, total, 300)
    items = [WorkList.model_validate(w, from_attributes=True) for w in works]
    data = {"total": total, "items": items}
    cache_set(ck, data, 300)
    return data


@router.get("/{work_id}", response_model=WorkRead)
async def get_work(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    svc = CurationService(db)
    work.curation_state = svc.work_state_payload(await svc.work_state(work_id))
    return work


@router.post("/{work_id}/favorite", response_model=WorkRead)
async def toggle_work_favorite(work_id: UUID, db: AsyncSession = Depends(get_db)):
    repo = WorkRepository(db)
    work = await repo.get(work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    before = bool(work.is_favorite)
    work.is_favorite = not work.is_favorite
    svc = CurationService(db)
    commit = await svc.record_work_favorite(work, before, bool(work.is_favorite))
    commit.stats = {"work_count": 1}
    await db.commit()
    await project_commit_safe(db, commit.id)
    await db.refresh(work)
    work.curation_state = svc.work_state_payload(await svc.work_state(work_id))
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
        "thumb_url": f"/media/thumb/{a.id}",
        "preview_url": signed_media_url(str(a.id), "preview"),
        "original_url": signed_media_url(str(a.id), "original"),
        "created_at": a.created_at.isoformat(),
    } for a in assets]


@router.delete("/{work_id}", status_code=204)
async def delete_work(work_id: UUID, db: AsyncSession = Depends(get_db)):
    """Move a work to trash. Does NOT delete database rows or files from disk."""
    work = await db.get(Work, work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    svc = CurationService(db)
    commit = await svc.trash_works([work_id], message=f"Move work to trash: {work.title or work_id}")
    await project_commit_safe(db, commit.id)
    cache_delete_pattern("works:*")


@router.post("/batch-delete")
async def batch_delete_works(data: dict, db: AsyncSession = Depends(get_db)):
    """Move multiple works to trash by ID list."""
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="ids list is required")
    svc = CurationService(db)
    commit = await svc.trash_works([UUID(wid) for wid in ids], message=f"Move {len(ids)} works to trash")
    await project_commit_safe(db, commit.id)
    cache_delete_pattern("works:*")
    return {
        "status": "ok",
        "deleted": int((commit.stats or {}).get("work_count", 0)),
        "commit_id": str(commit.id),
        "results": [{"id": wid, "status": "trashed"} for wid in ids],
    }


@router.post("/batch-curate", response_model=CurationCommitRead)
async def batch_curate_works(data: BatchCurateRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    if data.action == "trash":
        commit = await svc.trash_works(data.ids, reason=data.reason, message=data.message)
    elif data.action == "restore":
        commit = await svc.restore_works(data.ids, reason=data.reason, message=data.message)
    else:
        raise HTTPException(status_code=400, detail="action must be trash or restore")
    await project_commit_safe(db, commit.id)
    cache_delete_pattern("works:*")
    return await svc.commit_payload(commit.id)


@router.post("/batch-tag")
async def batch_tag_works(data: dict, db: AsyncSession = Depends(get_db)):
    """Add or remove tags on multiple works."""
    ids = data.get("ids", [])
    action = data.get("action", "add")  # add | remove
    tag_name = data.get("tag", "")
    if not ids or not tag_name:
        raise HTTPException(status_code=400, detail="ids and tag are required")

    from app.models.tag import Tag
    from app.models.work_tag import WorkTag

    # Find or create tag
    normalized = tag_name.strip().lower()
    result = await db.execute(select(Tag).where(Tag.normalized_name == normalized))
    tag = result.scalar_one_or_none()
    if not tag and action == "add":
        tag = Tag(normalized_name=normalized)
        db.add(tag)
        await db.flush()

    updated = 0
    for wid in ids:
        try:
            if action == "add" and tag:
                exists = await db.execute(
                    select(WorkTag).where(WorkTag.work_id == UUID(wid), WorkTag.tag_id == tag.id)
                )
                if not exists.scalar_one_or_none():
                    db.add(WorkTag(work_id=UUID(wid), tag_id=tag.id))
                    updated += 1
            elif action == "remove" and tag:
                await db.execute(
                    sql_delete(WorkTag).where(WorkTag.work_id == UUID(wid), WorkTag.tag_id == tag.id)
                )
                updated += 1
        except Exception:
            import logging
            logging.getLogger(__name__).warning("batch_tag_works failed for %s", wid, exc_info=True)
    await db.commit()
    return {"status": "ok", "updated": updated}
