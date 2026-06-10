from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from app.models.work import Work
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.work import WorkRead, WorkList, WorkListResponse
from app.repositories.work import WorkRepository
from app.services.cache import cache_get, cache_set, cache_key, cache_delete_pattern
from app.models.asset import Asset
from app.models.asset_source import AssetSource
from app.models.work import Work
from app.models.work_source import WorkSource
from app.models.tag import Tag
from app.models.work_tag import WorkTag

router = APIRouter(dependencies=[RequireAdmin])


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
    sort_by: str = "created_at",
    sort_order: str = "desc",
    db: AsyncSession = Depends(get_db),
):
    ck = cache_key("works:list", offset=offset, limit=limit,
                   search=search, source=source, creator_id=creator_id, tag=tag,
                   is_nsfw=is_nsfw, is_favorite=is_favorite,
                   is_ai_generated=is_ai_generated,
                   sort_by=sort_by, sort_order=sort_order)
    cached = cache_get(ck)
    if cached is not None:
        return cached
    repo = WorkRepository(db)
    works, total = await repo.list_all(
        offset=offset, limit=limit,
        search=search, source=source, creator_id=creator_id, tag=tag,
        is_nsfw=is_nsfw, is_favorite=is_favorite,
        is_ai_generated=is_ai_generated,
        sort_by=sort_by, sort_order=sort_order,
    )
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


@router.delete("/{work_id}", status_code=204)
async def delete_work(work_id: UUID, db: AsyncSession = Depends(get_db)):
    """Delete a work and all its related records. Does NOT delete files from disk."""
    work = await db.get(Work, work_id)
    if not work:
        raise HTTPException(status_code=404, detail="Work not found")
    await db.delete(work)
    await db.commit()
    cache_delete_pattern("works:*")
    # Remove from Meilisearch index (best-effort)
    try:
        from app.services.search import SearchService
        svc = SearchService(db)
        await svc.delete_work(str(work_id))
    except Exception:
        pass


@router.post("/batch-delete")
async def batch_delete_works(data: dict, db: AsyncSession = Depends(get_db)):
    """Delete multiple works and their assets by ID list."""
    ids = data.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="ids list is required")
    results = []
    for wid in ids:
        try:
            work = await db.get(Work, UUID(wid))
            if work:
                await db.delete(work)
                results.append({"id": wid, "status": "deleted"})
            else:
                results.append({"id": wid, "status": "not_found"})
        except Exception as e:
            results.append({"id": wid, "status": "error", "error": str(e)})
    await db.commit()
    # Remove from search index (best-effort per item)
    try:
        from app.services.search import SearchService
        svc = SearchService(db)
        for wid in ids:
            try:
                await svc.delete_work(str(wid))
            except Exception:
                pass
    except Exception:
        pass
    deleted = sum(1 for r in results if r["status"] == "deleted")
    return {"status": "ok", "deleted": deleted, "results": results}


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
            pass
    await db.commit()
    return {"status": "ok", "updated": updated}
