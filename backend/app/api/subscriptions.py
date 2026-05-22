import logging
from datetime import datetime, timezone, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.download_job import DownloadJob
from app.schemas.subscription import SubscriptionCreate, SubscriptionRead, SubscriptionUpdate
from app.schemas.subscription_source import SubscriptionSourceCreate, SubscriptionSourceRead, SubscriptionSourceUpdate
from app.services.subscription import SubscriptionService

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequireAdmin])


@router.get("", response_model=list[SubscriptionRead])
async def list_subscriptions(
    offset: int = 0, limit: int = 50,
    search: str | None = None,
    is_active: bool | None = None,
    sync_enabled: bool | None = None,
    never_synced: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    svc = SubscriptionService(db)
    return await svc.list_subscriptions(offset, limit,
                                        search=search, is_active=is_active,
                                        sync_enabled=sync_enabled,
                                        never_synced=never_synced)


# ── Batch Operations ──

@router.post("/batch-delete")
async def batch_delete_subscriptions(data: dict, db: AsyncSession = Depends(get_db)):
    ids = data.get("ids", [])
    svc = SubscriptionService(db)
    results = []
    for sid in ids:
        try:
            await svc.delete_subscription(UUID(sid))
            results.append({"id": sid, "status": "deleted"})
        except Exception as e:
            results.append({"id": sid, "status": "error", "error": str(e)})
    return {"status": "ok", "results": results}


@router.post("/batch-toggle-sync")
async def batch_toggle_sync(data: dict, db: AsyncSession = Depends(get_db)):
    """Enable/disable sync for multiple subscriptions."""
    ids = data.get("ids", [])
    enabled = data.get("sync_enabled", True)
    svc = SubscriptionService(db)
    results = []
    for sid in ids:
        try:
            await svc.update_subscription(UUID(sid), {"sync_enabled": enabled})
            results.append({"id": sid, "status": "updated", "sync_enabled": enabled})
        except Exception as e:
            results.append({"id": sid, "status": "error", "error": str(e)})
    return {"status": "ok", "results": results}


@router.post("/{subscription_id}/sync-now")
async def trigger_subscription_sync(subscription_id: UUID, db: AsyncSession = Depends(get_db)):
    """Manually trigger sync for a single subscription — creates download jobs for all enabled sources."""
    sub = await db.get(Subscription, subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sources = await db.execute(
        select(SubscriptionSource).where(
            and_(
                SubscriptionSource.subscription_id == subscription_id,
                SubscriptionSource.is_enabled == True,
            )
        )
    )
    sub_sources = sources.scalars().all()
    if not sub_sources:
        return {"status": "ok", "message": "No enabled sources", "job_ids": []}

    now = datetime.now(timezone.utc)
    job_ids = []

    for ss in sub_sources:
        if not ss.source_url:
            continue

        from app.providers import registry
        provider = registry.get(ss.source)
        if not provider or not provider.capabilities.can_download:
            continue
        if not provider.validate_url(ss.source_url):
            continue

        # Guard against duplicate: skip if recent job exists for this source
        interval_cutoff = now - timedelta(hours=sub.sync_interval_hours)
        recent = await db.execute(
            select(DownloadJob).where(
                and_(
                    DownloadJob.subscription_source_id == ss.id,
                    DownloadJob.status.in_(["pending", "downloading", "downloaded", "importing"]),
                    DownloadJob.created_at >= interval_cutoff,
                )
            ).limit(1)
        )
        if recent.scalar_one_or_none():
            continue

        job = DownloadJob(
            subscription_id=sub.id,
            subscription_source_id=ss.id,
            source=ss.source,
            source_url=ss.source_url,
            status="pending",
        )
        db.add(job)
        await db.flush()

        try:
            import redis as redis_lib
            from rq import Queue
            r = redis_lib.from_url(settings.redis_url)
            Queue(name="scheduled", connection=r).enqueue(
                "app.jobs.download.run_download_job", str(job.id))
            job_ids.append(str(job.id))
        except Exception as e:
            logger.error("Failed to enqueue download job %s: %s", job.id, e)

    if job_ids:
        sub.last_synced_at = now
    await db.commit()

    return {"status": "ok", "message": f"Enqueued {len(job_ids)} download jobs", "job_ids": job_ids}


@router.get("/{subscription_id}", response_model=SubscriptionRead)
async def get_subscription(subscription_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        return await svc.get_subscription(subscription_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", response_model=SubscriptionRead, status_code=201)
async def create_subscription(data: SubscriptionCreate, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    return await svc.create_subscription(data.model_dump())


@router.patch("/{subscription_id}", response_model=SubscriptionRead)
async def update_subscription(subscription_id: UUID, data: SubscriptionUpdate, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        return await svc.update_subscription(subscription_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{subscription_id}", status_code=204)
async def delete_subscription(subscription_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        await svc.delete_subscription(subscription_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{subscription_id}/sources", response_model=list[SubscriptionSourceRead])
async def list_subscription_sources(subscription_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    return await svc.list_sources(subscription_id)


@router.post("/{subscription_id}/sources", response_model=SubscriptionSourceRead, status_code=201)
async def add_subscription_source(
    subscription_id: UUID, data: SubscriptionSourceCreate, db: AsyncSession = Depends(get_db)
):
    svc = SubscriptionService(db)
    d = data.model_dump()
    d["subscription_id"] = subscription_id
    try:
        return await svc.add_source(d)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{subscription_id}/sources/{ss_id}", response_model=SubscriptionSourceRead)
async def update_subscription_source(
    subscription_id: UUID, ss_id: UUID, data: SubscriptionSourceUpdate, db: AsyncSession = Depends(get_db)
):
    svc = SubscriptionService(db)
    try:
        return await svc.update_source(ss_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{subscription_id}/sources/{ss_id}", status_code=204)
async def delete_subscription_source(subscription_id: UUID, ss_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        await svc.delete_source(ss_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
