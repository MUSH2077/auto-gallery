from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.creator import CreatorCreate, CreatorListResponse, CreatorRead, CreatorUpdate
from app.schemas.curation import CreatorCurationRequest, CurationCommitRead
from app.schemas.source_creator import SourceCreatorCreate, SourceCreatorRead
from app.schemas.creator_link import CreatorLinkCreate, CreatorLinkRead, CreatorLinkUpdate
from app.models.creator import Creator
from app.services.creator import CreatorService
from app.services.cache import (
    cache_get,
    cache_set,
    cache_key,
    TTL,
    invalidate_api_caches,
    invalidate_creator_subscription_caches,
)
from app.services.curation import CurationService

router = APIRouter(dependencies=[RequireAdmin])

@router.get("/count")
async def count_creators(db: AsyncSession = Depends(get_db)):
    """Return total number of creators."""
    ck = cache_key("creators:count")
    cached = cache_get(ck)
    if cached is not None:
        return cached
    result = await db.execute(select(func.count()).select_from(Creator))
    data = {"count": result.scalar() or 0}
    cache_set(ck, data, TTL["creators:count"])
    return data


@router.get("", response_model=CreatorListResponse)
async def list_creators(
    offset: int = 0, limit: int = 50,
    search: str | None = None,
    is_active: bool | None = None,
    has_danbooru: bool | None = None,
    has_subscription: bool | None = None,
    is_favorite: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    ck = cache_key("creators:list", offset=offset, limit=limit,
                   search=search, is_active=is_active,
                   has_danbooru=has_danbooru,
                   has_subscription=has_subscription,
                   is_favorite=is_favorite)
    cached = cache_get(ck)
    if cached is not None:
        return cached
    svc = CreatorService(db)
    data = await svc.list_creators(offset, limit,
                                   search=search, is_active=is_active,
                                   has_danbooru=has_danbooru,
                                   has_subscription=has_subscription,
                                   is_favorite=is_favorite)
    cache_set(ck, data, TTL["creators:list"])
    return data


# ── Batch Operations ──

@router.post("/batch-delete")
async def batch_delete_creators(data: dict, db: AsyncSession = Depends(get_db)):
    """Delete multiple creators by ID list."""
    ids = data.get("ids", [])
    svc = CreatorService(db)
    results = []
    for cid in ids:
        try:
            await svc.delete_creator(UUID(cid))
            results.append({"id": cid, "status": "deleted"})
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("batch_delete_creators failed for %s: %s", cid, e, exc_info=True)
            results.append({"id": cid, "status": "error", "error": "internal_error"})
    invalidate_creator_subscription_caches(include_works=True)
    return {"status": "ok", "results": results}


# ── Dedup & Merge ──

@router.get("/duplicates")
async def list_duplicate_creators(db: AsyncSession = Depends(get_db)):
    """List potential duplicate creators for admin review."""
    from app.services.creator_dedup import find_merge_candidates
    candidates = await find_merge_candidates(db)
    return {"duplicates": candidates, "total": len(candidates)}


@router.post("/merge")
async def merge_creators_endpoint(data: dict, db: AsyncSession = Depends(get_db)):
    """Merge source creators into target. Source creators are deleted after merge.

    Accepts: {target_id: UUID, source_ids: [UUID, ...]}
    """
    from app.services.creator_dedup import merge_creators

    target_id = UUID(data["target_id"])
    source_ids = data.get("source_ids", [])
    results = []
    for source_id_str in source_ids:
        source_id = UUID(source_id_str)
        try:
            stats = await merge_creators(db, target_id, source_id)
            results.append({"source_id": source_id_str, "status": "merged", **stats})
        except ValueError as e:
            results.append({"source_id": source_id_str, "status": "error", "error": str(e)})
    invalidate_creator_subscription_caches(include_works=True)
    return {"status": "ok", "results": results}


@router.get("/{creator_id}", response_model=CreatorRead)
async def get_creator(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        creator = await svc.get_creator(creator_id)
        curation = CurationService(db)
        creator.curation_state = curation.creator_state_payload(await curation.creator_state(creator_id))
        return creator
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", response_model=CreatorRead, status_code=201)
async def create_creator(data: CreatorCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    result = await svc.create_creator(data.model_dump())
    invalidate_api_caches("creators")
    return result


@router.patch("/{creator_id}", response_model=CreatorRead)
async def update_creator(creator_id: UUID, data: CreatorUpdate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        result = await svc.update_creator(creator_id, data.model_dump(exclude_none=True))
        invalidate_api_caches("creators")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))




@router.get("/{creator_id}/timeline")
async def get_creator_timeline(creator_id: UUID,
                                from_date: str | None = None,
                                to_date: str | None = None,
                                db: AsyncSession = Depends(get_db)):
    """Return per-source work counts per day for the creator's activity grid."""
    from app.repositories.work import WorkRepository
    repo = WorkRepository(db)
    days, sources = await repo.get_creator_timeline(creator_id, from_date, to_date)
    return {
        "creator_id": str(creator_id),
        "sources": sources,
        "days": days,
        "total": sum(d["total"] for d in days),
    }

@router.get("/{creator_id}/stats")
async def get_creator_stats(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """Return creator statistics: tag distribution, source breakdown, monthly posting frequency."""
    ck = cache_key("creators:stats", creator_id=str(creator_id))
    cached = cache_get(ck)
    if cached is not None:
        return cached
    from app.models.work_source import WorkSource
    from app.models.work_tag import WorkTag
    from app.models.tag import Tag
    from app.models.source_creator import SourceCreator
    from sqlalchemy import func, and_

    # Works per source
    ws_rows = await db.execute(
        select(WorkSource.source, func.count(WorkSource.id).label("cnt"))
        .join(SourceCreator, and_(
            SourceCreator.source == WorkSource.source,
            SourceCreator.source_creator_id == WorkSource.source_creator_id))
        .where(SourceCreator.creator_id == creator_id)
        .group_by(WorkSource.source)
        .order_by(func.count(WorkSource.id).desc())
    )
    source_breakdown = [{"source": row[0], "count": row[1]} for row in ws_rows]

    # Top tags
    tag_rows = await db.execute(
        select(Tag.normalized_name, func.count(WorkTag.work_id).label("cnt"))
        .join(WorkTag, WorkTag.tag_id == Tag.id)
        .join(WorkSource, WorkSource.work_id == WorkTag.work_id)
        .join(SourceCreator, and_(
            SourceCreator.source == WorkSource.source,
            SourceCreator.source_creator_id == WorkSource.source_creator_id))
        .where(SourceCreator.creator_id == creator_id)
        .group_by(Tag.normalized_name)
        .order_by(func.count(WorkTag.work_id).desc())
        .limit(20)
    )
    tag_distribution = [{"tag": row[0], "count": row[1]} for row in tag_rows]

    # Monthly posting frequency
    month_rows = await db.execute(
        select(
            func.to_char(func.date_trunc("month", WorkSource.posted_at), "YYYY-MM").label("month"),
            func.count(WorkSource.id).label("cnt"))
        .join(SourceCreator, and_(
            SourceCreator.source == WorkSource.source,
            SourceCreator.source_creator_id == WorkSource.source_creator_id))
        .where(SourceCreator.creator_id == creator_id)
        .where(WorkSource.posted_at.isnot(None))
        .group_by("month")
        .order_by("month")
    )
    monthly = [{"month": row[0], "count": row[1]} for row in month_rows]

    # Totals
    total_works = sum(s["count"] for s in source_breakdown)
    total_tags = len(tag_distribution)

    # Asset count
    from app.models.asset_source import AssetSource
    asset_result = await db.execute(
        select(func.count(AssetSource.id))
        .select_from(WorkSource)
        .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
        .join(SourceCreator, and_(
            SourceCreator.source == WorkSource.source,
            SourceCreator.source_creator_id == WorkSource.source_creator_id))
        .where(SourceCreator.creator_id == creator_id)
    )
    total_assets = asset_result.scalar() or 0

    data = {
        "creator_id": str(creator_id),
        "total_works": total_works,
        "total_assets": total_assets,
        "total_tags": total_tags,
        "source_breakdown": source_breakdown,
        "tag_distribution": tag_distribution,
        "monthly_frequency": monthly,
    }
    cache_set(ck, data, TTL["creators:stats"])
    return data


@router.get("/{creator_id}/subscription-overview")
async def get_creator_subscription_overview(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """Return subscription sources as GitHub-like repository candidates for a creator."""
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.models.download_job import DownloadJob
    from app.providers import registry

    sub_rows = await db.execute(
        select(Subscription)
        .where(Subscription.creator_id == creator_id)
        .order_by(Subscription.created_at.desc())
    )
    subscriptions = list(sub_rows.scalars().all())
    subscription_ids = [s.id for s in subscriptions]

    if not subscription_ids:
        return {
            "creator_id": str(creator_id),
            "subscriptions": [],
            "repositories": [],
            "summary": {
                "subscription_count": 0,
                "repository_count": 0,
                "enabled_repository_count": 0,
                "running_job_count": 0,
            },
        }

    source_rows = await db.execute(
        select(SubscriptionSource)
        .where(SubscriptionSource.subscription_id.in_(subscription_ids))
        .order_by(SubscriptionSource.source, SubscriptionSource.created_at.desc())
    )
    sub_sources = list(source_rows.scalars().all())

    repositories = []
    running_statuses = {"enqueued", "downloading", "downloaded", "importing"}
    running_job_count = 0

    for ss in sub_sources:
        can_download = False
        supports_gallerydl = False
        url_valid = False
        provider_name = ss.source
        provider_display_name = ss.source

        try:
            provider = registry.get(ss.source)
            provider_name = provider.source_name
            provider_display_name = provider.display_name
            can_download = bool(provider.capabilities.can_download)
            supports_gallerydl = bool(provider.capabilities.supports_gallerydl)
            normalized_url = provider.normalize_url(ss.source_url) if ss.source_url else None
            url_valid = bool(ss.source_url and provider.validate_url(normalized_url or ss.source_url))
        except Exception:
            url_valid = False

        latest_job_row = await db.execute(
            select(DownloadJob)
            .where(DownloadJob.subscription_source_id == ss.id)
            .order_by(DownloadJob.created_at.desc())
            .limit(1)
        )
        latest_job = latest_job_row.scalar_one_or_none()
        latest_job_payload = None
        if latest_job:
            if latest_job.status in running_statuses:
                running_job_count += 1
            latest_job_payload = {
                "id": str(latest_job.id),
                "status": latest_job.status,
                "created_at": latest_job.created_at.isoformat() if latest_job.created_at else None,
                "updated_at": latest_job.updated_at.isoformat() if latest_job.updated_at else None,
                "error_log_excerpt": (latest_job.error_log or "")[:240] or None,
            }

        is_repository = bool(can_download and url_valid and ss.source_url)
        repositories.append({
            "id": str(ss.id),
            "subscription_id": str(ss.subscription_id),
            "source": provider_name,
            "source_display_name": provider_display_name,
            "source_url": ss.source_url,
            "source_creator_id": ss.source_creator_id,
            "is_enabled": ss.is_enabled,
            "auth_healthy": ss.auth_healthy,
            "auth_status": ss.auth_status,
            "auth_error_reason": ss.auth_error_reason,
            "last_auth_checked_at": ss.last_auth_checked_at.isoformat() if ss.last_auth_checked_at else None,
            "last_successful_auth": ss.last_successful_auth.isoformat() if ss.last_successful_auth else None,
            "last_synced_at": ss.last_synced_at.isoformat() if ss.last_synced_at else None,
            "last_attempted_at": ss.last_attempted_at.isoformat() if ss.last_attempted_at else None,
            "can_download": can_download,
            "supports_gallerydl": supports_gallerydl,
            "url_valid": url_valid,
            "is_repository": is_repository,
            "latest_job": latest_job_payload,
            "created_at": ss.created_at.isoformat() if ss.created_at else None,
            "updated_at": ss.updated_at.isoformat() if ss.updated_at else None,
        })

    return {
        "creator_id": str(creator_id),
        "subscriptions": [{
            "id": str(s.id),
            "name": s.name,
            "is_active": s.is_active,
            "sync_enabled": s.sync_enabled,
            "sync_interval_hours": s.sync_interval_hours,
            "schedule_mode": s.schedule_mode,
            "scheduled_times": s.scheduled_times,
            "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        } for s in subscriptions],
        "repositories": repositories,
        "summary": {
            "subscription_count": len(subscriptions),
            "repository_count": sum(1 for r in repositories if r["is_repository"]),
            "enabled_repository_count": sum(1 for r in repositories if r["is_repository"] and r["is_enabled"]),
            "running_job_count": running_job_count,
        },
    }


@router.delete("/{creator_id}", status_code=204)
async def delete_creator(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        await svc.delete_creator(creator_id)
        invalidate_creator_subscription_caches(include_works=True)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{creator_id}/favorite", response_model=CreatorRead)
async def toggle_creator_favorite(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        result = await svc.toggle_favorite(creator_id)
        invalidate_api_caches("creators")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{creator_id}/curation", response_model=CurationCommitRead)
async def curate_creator(creator_id: UUID, data: CreatorCurationRequest, db: AsyncSession = Depends(get_db)):
    svc = CurationService(db)
    commit = await svc.curate_creator(
        creator_id,
        action=data.action,
        reason=data.reason,
        message=data.message,
    )
    invalidate_api_caches("creators", "works")
    return await svc.commit_payload(commit.id)


@router.get("/{creator_id}/sources", response_model=list[SourceCreatorRead])
async def list_source_creators(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    """List source accounts for a creator."""
    from sqlalchemy import select
    from app.models.source_creator import SourceCreator as SourceCreatorModel
    result = await db.execute(
        select(SourceCreatorModel).where(
            SourceCreatorModel.creator_id == creator_id,
        ).order_by(SourceCreatorModel.source, SourceCreatorModel.source_creator_id)
    )
    return result.scalars().all()


@router.post("/{creator_id}/sources", response_model=SourceCreatorRead, status_code=201)
async def add_source_creator(creator_id: UUID, data: SourceCreatorCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    d = data.model_dump()
    d["creator_id"] = creator_id
    result = await svc.add_source_creator(d)
    invalidate_api_caches("creators", "works")
    return result


@router.get("/{creator_id}/links", response_model=list[CreatorLinkRead])
async def list_creator_links(creator_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    return await svc.list_links(creator_id)


@router.post("/{creator_id}/links", response_model=CreatorLinkRead, status_code=201)
async def add_creator_link(creator_id: UUID, data: CreatorLinkCreate, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    d = data.model_dump()
    d["creator_id"] = creator_id
    return await svc.add_link(d)


@router.patch("/{creator_id}/links/{link_id}", response_model=CreatorLinkRead)
async def update_creator_link(
    creator_id: UUID, link_id: UUID, data: CreatorLinkUpdate, db: AsyncSession = Depends(get_db)
):
    svc = CreatorService(db)
    try:
        return await svc.update_link(link_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.delete("/{creator_id}/links/{link_id}", status_code=204)
async def delete_creator_link(creator_id: UUID, link_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CreatorService(db)
    try:
        await svc.delete_link(link_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
