from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin
from app.database import get_db
from app.models.asset_source import AssetSource
from app.models.creator import Creator
from app.models.download_job import DownloadJob
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.work import Work
from app.models.curation import WorkCurationState
from app.models.work_source import WorkSource
from app.providers import registry
from app.schemas.curation import RepositoryGraphResponse
from app.services.curation import CurationService
from app.services.subscription_enqueue import enqueue_subscription_source_sync

router = APIRouter(dependencies=[RequireAdmin])

RUNNING_STATUSES = {"pending", "downloading", "downloaded", "importing"}


def _provider_payload(ss: SubscriptionSource) -> dict:
    provider_name = ss.source
    provider_display_name = ss.source
    normalized_url = ss.source_url
    url_valid = False
    capabilities = {
        "can_download": False,
        "can_import_local": False,
        "supports_gallerydl": False,
        "supports_tags": False,
        "is_reference_only": False,
    }

    try:
        provider = registry.get(ss.source)
        provider_name = provider.source_name
        provider_display_name = provider.display_name
        normalized_url = provider.normalize_url(ss.source_url) if ss.source_url else None
        candidate_url = normalized_url or ss.source_url
        url_valid = bool(candidate_url and provider.validate_url(candidate_url))
        capabilities = {
            "can_download": bool(provider.capabilities.can_download),
            "can_import_local": bool(provider.capabilities.can_import_local),
            "supports_gallerydl": bool(provider.capabilities.supports_gallerydl),
            "supports_tags": bool(provider.capabilities.supports_tags),
            "is_reference_only": bool(provider.capabilities.is_reference_only),
        }
    except Exception:
        pass

    return {
        "source": provider_name,
        "display_name": provider_display_name,
        "normalized_url": normalized_url,
        "url_valid": url_valid,
        "capabilities": capabilities,
    }


def _job_payload(job: DownloadJob) -> dict:
    return {
        "id": str(job.id),
        "subscription_id": str(job.subscription_id),
        "subscription_source_id": str(job.subscription_source_id) if job.subscription_source_id else None,
        "source": job.source,
        "source_url": job.source_url,
        "status": job.status,
        "retry_count": job.retry_count,
        "error_log_excerpt": (job.error_log or "")[:240] or None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _repository_payload(ss: SubscriptionSource, provider: dict, latest_job: DownloadJob | None) -> dict:
    latest_job_payload = _job_payload(latest_job) if latest_job else None
    return {
        "id": str(ss.id),
        "subscription_id": str(ss.subscription_id),
        "source": provider["source"],
        "source_display_name": provider["display_name"],
        "source_creator_id": ss.source_creator_id,
        "source_url": ss.source_url,
        "is_enabled": ss.is_enabled,
        "auth_healthy": ss.auth_healthy,
        "auth_status": ss.auth_status,
        "auth_error_reason": ss.auth_error_reason,
        "last_auth_checked_at": ss.last_auth_checked_at.isoformat() if ss.last_auth_checked_at else None,
        "last_successful_auth": ss.last_successful_auth.isoformat() if ss.last_successful_auth else None,
        "last_synced_at": ss.last_synced_at.isoformat() if ss.last_synced_at else None,
        "last_attempted_at": ss.last_attempted_at.isoformat() if ss.last_attempted_at else None,
        "can_download": bool(provider["capabilities"]["can_download"]),
        "supports_gallerydl": bool(provider["capabilities"]["supports_gallerydl"]),
        "url_valid": bool(provider["url_valid"]),
        "is_repository": bool(provider["capabilities"]["can_download"] and provider["url_valid"] and ss.source_url),
        "latest_job": latest_job_payload,
        "created_at": ss.created_at.isoformat() if ss.created_at else None,
        "updated_at": ss.updated_at.isoformat() if ss.updated_at else None,
    }


async def _get_source_context(db: AsyncSession, source_id: UUID):
    result = await db.execute(
        select(SubscriptionSource, Subscription, Creator)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .join(Creator, Creator.id == Subscription.creator_id)
        .where(SubscriptionSource.id == source_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Repository not found")
    return row


@router.get("/{source_id}")
async def get_repository(source_id: UUID, db: AsyncSession = Depends(get_db)):
    ss, sub, creator = await _get_source_context(db, source_id)
    provider = _provider_payload(ss)

    jobs_result = await db.execute(
        select(DownloadJob)
        .where(DownloadJob.subscription_source_id == ss.id)
        .order_by(DownloadJob.created_at.desc())
        .limit(10)
    )
    recent_jobs = list(jobs_result.scalars().all())
    latest_job = recent_jobs[0] if recent_jobs else None

    recent_works = []
    if ss.source_creator_id:
        asset_count_sq = (
            select(AssetSource.work_source_id, func.count(AssetSource.asset_id).label("asset_count"))
            .group_by(AssetSource.work_source_id)
            .subquery()
        )
        works_result = await db.execute(
            select(Work, WorkSource, func.coalesce(asset_count_sq.c.asset_count, 1))
            .join(WorkSource, WorkSource.work_id == Work.id)
            .outerjoin(asset_count_sq, asset_count_sq.c.work_source_id == WorkSource.id)
            .outerjoin(WorkCurationState, WorkCurationState.work_id == Work.id)
            .where(
                WorkSource.source == ss.source,
                WorkSource.source_creator_id == ss.source_creator_id,
                (WorkCurationState.visibility.is_(None)) | (WorkCurationState.visibility == "visible"),
            )
            .order_by(WorkSource.posted_at.desc(), Work.created_at.desc())
            .limit(12)
        )
        for work, work_source, asset_count in works_result:
            recent_works.append({
                "id": str(work.id),
                "title": work.title or work_source.title,
                "posted_at": _iso(work_source.posted_at or work.posted_at),
                "thumbnail_asset_id": str(work.thumbnail_asset_id) if work.thumbnail_asset_id else None,
                "asset_count": int(asset_count or 1),
                "is_nsfw": work.is_nsfw,
                "is_ai_generated": work.is_ai_generated,
                "is_favorite": work.is_favorite,
                "created_at": work.created_at.isoformat() if work.created_at else None,
                "source": work_source.source,
                "creator_name": creator.display_name or creator.name,
                "creator_id": str(creator.id),
            })

    return {
        "repository": _repository_payload(ss, provider, latest_job),
        "creator": {
            "id": str(creator.id),
            "name": creator.name,
            "display_name": creator.display_name,
            "thumbnail_url": creator.thumbnail_url,
            "is_favorite": creator.is_favorite,
        },
        "subscription": {
            "id": str(sub.id),
            "name": sub.name,
            "is_active": sub.is_active,
            "sync_enabled": sub.sync_enabled,
            "sync_interval_hours": sub.sync_interval_hours,
            "schedule_mode": sub.schedule_mode,
            "scheduled_times": sub.scheduled_times,
            "last_synced_at": sub.last_synced_at.isoformat() if sub.last_synced_at else None,
        },
        "provider": provider,
        "recent_jobs": [_job_payload(job) for job in recent_jobs],
        "recent_works": recent_works,
    }


@router.post("/{source_id}/sync-now")
async def sync_repository(source_id: UUID, db: AsyncSession = Depends(get_db)):
    await _get_source_context(db, source_id)
    return await enqueue_subscription_source_sync(db, source_id, trigger="manual_repository")


@router.get("/{source_id}/curation-graph", response_model=RepositoryGraphResponse)
async def get_repository_curation_graph(
    source_id: UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    trigger: str | None = None,
    include_baseline: bool = True,
    db: AsyncSession = Depends(get_db),
):
    await _get_source_context(db, source_id)
    svc = CurationService(db)
    return await svc.repository_graph(source_id, offset=offset, limit=limit, trigger=trigger, include_baseline=include_baseline)
