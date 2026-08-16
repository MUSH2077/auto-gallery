from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.models.asset import Asset
from app.models.asset_source import AssetSource
from app.models.creator import Creator
from app.models.download_job import DownloadJob
from app.models.repository_sync_receipt import RepositorySyncReceipt
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.work import Work
from app.models.curation import WorkCurationState
from app.models.work_source import WorkSource
from app.models.work_source_tag import WorkSourceTag
from app.models.tag import Tag
from app.providers import registry
from app.schemas.curation import RepositoryGraphResponse
from app.schemas.deletion import (
    BatchDeletionRequest,
    DeletionPreviewResponse,
    DeletionResultResponse,
)
from app.services.curation import CurationService
from app.services.subscription_enqueue import enqueue_subscription_source_sync
from app.services.repository_identity import resolve_repository_source_creator_ids
from app.services.sync_outcome import download_job_outcome

router = APIRouter(dependencies=[RequirePermission("library")])
mutation_router = APIRouter()

RUNNING_STATUSES = {"enqueued", "downloading", "downloaded", "importing"}


@mutation_router.post(
    "/batch-deletion-preview",
    response_model=DeletionPreviewResponse,
)
async def batch_repository_deletion_preview(
    data: BatchDeletionRequest,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    scope = await HierarchicalDeletionService(db).scope("repository", data.ids)
    return scope.preview_payload(is_admin=user.is_admin)


@mutation_router.post(
    "/batch-delete",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def batch_delete_repositories(
    data: BatchDeletionRequest,
    response: Response,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.cache import invalidate_creator_subscription_caches
    from app.services.hierarchical_deletion import (
        HierarchicalDeletionService,
        enqueue_permanent_deletion,
    )

    if data.delete_files and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Administrator access required to delete files",
        )
    service = HierarchicalDeletionService(db)
    scope = await service.scope("repository", data.ids)
    service.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "repository", data.ids, delete_files=data.delete_files
        )
    else:
        result = await service.soft_delete(scope)
    invalidate_creator_subscription_caches(include_works=True)
    return result


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
        "outcome": download_job_outcome(job),
        "error_log_excerpt": (job.error_log or "")[:240] or None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _receipt_payload(receipt: RepositorySyncReceipt, ss: SubscriptionSource) -> dict:
    outcome = None
    if isinstance(receipt.detail, dict) and isinstance(receipt.detail.get("outcome"), dict):
        outcome = receipt.detail["outcome"]
    return {
        "id": str(receipt.id),
        "receipt_id": str(receipt.id),
        "task_id": None,
        "original_task_id": str(receipt.source_task_id) if receipt.source_task_id else None,
        "download_job_id": str(receipt.source_download_job_id),
        "import_job_id": str(receipt.source_import_job_id) if receipt.source_import_job_id else None,
        "subscription_id": str(ss.subscription_id),
        "subscription_source_id": str(receipt.repository_id),
        "source": receipt.source,
        "source_url": ss.source_url,
        "status": receipt.status,
        "outcome_code": receipt.outcome_code,
        "retry_count": receipt.attempts,
        "attempts": receipt.attempts,
        "metadata_count": receipt.metadata_count,
        "media_count": receipt.media_count,
        "works_imported": receipt.works_imported,
        "duration_ms": receipt.duration_ms,
        "error_code": receipt.error_code,
        "error_log_excerpt": receipt.error_excerpt,
        "recovered": receipt.recovered,
        "recovered_at": _iso(receipt.recovered_at),
        "outcome": outcome,
        "started_at": _iso(receipt.started_at),
        "finished_at": _iso(receipt.finished_at),
        "created_at": _iso(receipt.finished_at),
        "updated_at": _iso(receipt.updated_at),
        "record_type": "sync_receipt",
    }


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _repository_payload(ss: SubscriptionSource, provider: dict, latest_job_payload: dict | None) -> dict:
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


@mutation_router.get("/{source_id}/deletion-preview", response_model=DeletionPreviewResponse)
async def repository_deletion_preview(
    source_id: UUID,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    scope = await HierarchicalDeletionService(db).scope("repository", [source_id])
    return scope.preview_payload(is_admin=user.is_admin)


@mutation_router.delete(
    "/{source_id}",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def delete_repository(
    source_id: UUID,
    response: Response,
    delete_files: bool = Query(False),
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import (
        HierarchicalDeletionService,
        enqueue_permanent_deletion,
    )

    if delete_files and not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required to delete files")
    svc = HierarchicalDeletionService(db)
    scope = await svc.scope("repository", [source_id])
    svc.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "repository", [source_id], delete_files=delete_files
        )
    else:
        result = await svc.soft_delete(scope)
    return result


@router.get("/{source_id}")
async def get_repository(source_id: UUID, db: AsyncSession = Depends(get_db)):
    ss, sub, creator = await _get_source_context(db, source_id)
    provider = _provider_payload(ss)

    jobs_result = await db.execute(
        select(DownloadJob)
        .where(
            DownloadJob.subscription_source_id == ss.id,
            DownloadJob.status.in_(RUNNING_STATUSES),
        )
        .order_by(DownloadJob.created_at.desc())
        .limit(10)
    )
    active_jobs = list(jobs_result.scalars().all())
    receipts_result = await db.execute(
        select(RepositorySyncReceipt)
        .where(RepositorySyncReceipt.repository_id == ss.id)
        .order_by(
            RepositorySyncReceipt.finished_at.desc(),
            RepositorySyncReceipt.id.desc(),
        )
        .limit(50)
    )
    sync_receipts = list(receipts_result.scalars().all())
    sync_history = [_receipt_payload(receipt, ss) for receipt in sync_receipts]
    latest_job_payload = (
        _job_payload(active_jobs[0])
        if active_jobs
        else (sync_history[0] if sync_history else None)
    )

    source_creator_ids = await resolve_repository_source_creator_ids(db, ss, creator.id)
    recent_works = []
    if source_creator_ids:
        asset_count_sq = (
            select(
                AssetSource.work_source_id,
                func.count(AssetSource.asset_id).label("asset_count"),
                (
                    func.count(AssetSource.asset_id).filter(or_(
                        Asset.mime_type.like("video/%"),
                        Asset.file_name.ilike("%.mp4"),
                        Asset.file_name.ilike("%.webm"),
                    )) > 0
                ).label("has_video"),
            )
            .join(Asset, Asset.id == AssetSource.asset_id)
            .group_by(AssetSource.work_source_id)
            .subquery()
        )
        works_result = await db.execute(
            select(
                Work,
                WorkSource,
                func.coalesce(asset_count_sq.c.asset_count, 1),
                func.coalesce(asset_count_sq.c.has_video, False),
            )
            .join(WorkSource, WorkSource.work_id == Work.id)
            .outerjoin(asset_count_sq, asset_count_sq.c.work_source_id == WorkSource.id)
            .outerjoin(WorkCurationState, WorkCurationState.work_id == Work.id)
            .where(
                WorkSource.source == ss.source,
                WorkSource.source_creator_id.in_(source_creator_ids),
                (WorkCurationState.visibility.is_(None)) | (WorkCurationState.visibility == "visible"),
            )
            .order_by(WorkSource.posted_at.desc(), Work.created_at.desc())
            .limit(12)
        )
        for work, work_source, asset_count, has_video in works_result:
            recent_works.append({
                "id": str(work.id),
                "title": work.title or work_source.title,
                "posted_at": _iso(work_source.posted_at or work.posted_at),
                "thumbnail_asset_id": str(work.thumbnail_asset_id) if work.thumbnail_asset_id else None,
                "asset_count": int(asset_count or 1),
                "has_video": bool(has_video),
                "is_nsfw": work.is_nsfw,
                "is_ai_generated": work.is_ai_generated,
                "is_favorite": work.is_favorite,
                "created_at": work.created_at.isoformat() if work.created_at else None,
                "source": work_source.source,
                "creator_name": creator.display_name or creator.name,
                "creator_id": str(creator.id),
            })

    return {
        "repository": _repository_payload(ss, provider, latest_job_payload),
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
        # Compatibility view for older clients. Successful history is now a
        # compact domain receipt and does not depend on TaskRun/DownloadJob.
        "recent_jobs": sync_history,
        "active_jobs": [_job_payload(job) for job in active_jobs],
        "sync_history": sync_history,
        "recent_works": recent_works,
    }


@router.get("/{source_id}/tags")
async def get_repository_tags(
    source_id: UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    ss, _, creator = await _get_source_context(db, source_id)
    source_creator_ids = await resolve_repository_source_creator_ids(db, ss, creator.id)
    if not source_creator_ids:
        return {"items": [], "total": 0}

    filters = (
        WorkSource.source == ss.source,
        WorkSource.source_creator_id.in_(source_creator_ids),
    )
    total = int((await db.execute(
        select(func.count(func.distinct(WorkSourceTag.tag_id)))
        .select_from(WorkSourceTag)
        .join(WorkSource, WorkSource.id == WorkSourceTag.work_source_id)
        .where(*filters)
    )).scalar() or 0)

    rows = (await db.execute(
        select(
            Tag,
            func.count(func.distinct(WorkSource.work_id)).label("usage_count"),
        )
        .join(WorkSourceTag, WorkSourceTag.tag_id == Tag.id)
        .join(WorkSource, WorkSource.id == WorkSourceTag.work_source_id)
        .where(*filters)
        .group_by(Tag.id)
        .order_by(
            func.count(func.distinct(WorkSource.work_id)).desc(),
            Tag.normalized_name,
        )
        .offset(offset)
        .limit(limit)
    )).all()
    return {
        "items": [{
            "id": str(tag.id),
            "normalized_name": tag.normalized_name,
            "category": tag.category,
            "usage_count": int(usage_count or 0),
            "created_at": tag.created_at.isoformat() if tag.created_at else None,
        } for tag, usage_count in rows],
        "total": total,
    }


@router.post("/{source_id}/sync-now")
async def sync_repository(source_id: UUID, db: AsyncSession = Depends(get_db)):
    await _get_source_context(db, source_id)
    result = await enqueue_subscription_source_sync(
        db,
        source_id,
        trigger="manual_repository",
        force=False,
    )
    reason = result.get("reason") or {}
    code = result.get("skip_reason") or reason.get("code")
    hard_gate_codes = {
        "queue_saturated",
        "enqueue_busy",
        "redis_capacity",
        "redis_unwritable",
        "disk_backpressure",
        "import_backpressure",
        "storage_unavailable",
        "admission_check_failed",
        "enqueue_failed",
    }
    if code in hard_gate_codes:
        detail = {
            "code": code,
            "message": reason.get("message") or result.get("error") or str(code).replace("_", " "),
            **(reason.get("details") or {}),
        }
        raise HTTPException(
            status_code=429 if code == "queue_saturated" else 503,
            detail=detail,
        )
    return result


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
