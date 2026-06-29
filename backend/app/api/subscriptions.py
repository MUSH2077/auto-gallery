import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.schemas.subscription import SubscriptionCreate, SubscriptionRead, SubscriptionUpdate
from app.schemas.subscription_source import SubscriptionSourceCreate, SubscriptionSourceRead, SubscriptionSourceUpdate
from app.services.subscription_enqueue import enqueue_subscription_source_sync
from app.services.subscription import SubscriptionService
from app.services.tasks import TaskService
from app.services.cache import (
    cache_get,
    cache_set,
    cache_key,
    TTL,
    invalidate_api_caches,
    invalidate_creator_subscription_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequireAdmin])



@router.get("/count")
async def count_subscriptions(db: AsyncSession = Depends(get_db)):
    """Return total number of subscriptions."""
    result = await db.execute(select(func.count()).select_from(Subscription))
    return {"count": result.scalar() or 0}


@router.get("", response_model=list[SubscriptionRead])
async def list_subscriptions(
    offset: int = 0, limit: int = 50,
    search: str | None = None,
    is_active: bool | None = None,
    sync_enabled: bool | None = None,
    never_synced: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    ck = cache_key("subscriptions:list", offset=offset, limit=limit,
                   search=search, is_active=is_active,
                   sync_enabled=sync_enabled, never_synced=never_synced)
    cached = cache_get(ck)
    if cached is not None:
        return cached
    svc = SubscriptionService(db)
    subs = await svc.list_subscriptions(offset, limit,
                                        search=search, is_active=is_active,
                                        sync_enabled=sync_enabled,
                                        never_synced=never_synced)
    items = [SubscriptionRead.model_validate(s, from_attributes=True) for s in subs]
    cache_set(ck, items, TTL["subscriptions:list"])
    return items


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
    invalidate_creator_subscription_caches()
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
    invalidate_api_caches("subscriptions", "creators")
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

    task_service = TaskService(db)
    parent_task = await task_service.create_task(
        kind="admin",
        operation_type="subscription-sync-batch",
        title=f"Sync subscription {subscription_id}",
        status="running",
        queue_name="downloads",
        progress={"phase": "scanning", "label": "Checking subscription sources", "current": 0, "total": len(sub_sources)},
        meta={"subscription_id": str(subscription_id), "scope": "subscription"},
    )

    job_ids = []
    task_ids = []
    skipped = []
    errors = []

    for index, ss in enumerate(sub_sources, start=1):
        result = await enqueue_subscription_source_sync(
            db,
            ss.id,
            trigger="manual_subscription",
            force=False,
            parent_task_id=parent_task.id,
            force_reason="subscription_sync_now",
        )
        if result["status"] == "enqueued":
            job_ids.append(result["job_id"])
            if result.get("task_id"):
                task_ids.append(result["task_id"])
        elif result["status"] == "error":
            errors.append(result)
        else:
            skipped.append(result)
        await task_service.update_task(
            parent_task,
            progress={"phase": "enqueuing", "label": "Enqueuing source downloads", "current": index, "total": len(sub_sources)},
        )

    summary = {
        "enqueued_count": len(job_ids),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "job_ids": job_ids,
        "task_ids": task_ids,
        "skipped": skipped,
        "errors": errors,
    }
    if errors and not job_ids:
        await task_service.update_task(parent_task, status="failed", result=summary, error="All enqueue attempts failed")
        await db.commit()
        return {"status": "error", "message": "All enqueue attempts failed", "task_id": str(parent_task.id), **summary}
    if errors:
        invalidate_api_caches("subscriptions", "creators")
        await task_service.update_task(parent_task, status="complete", result=summary, progress={"phase": "complete", "label": "Subscription sync enqueued", "current": len(sub_sources), "total": len(sub_sources)})
        await db.commit()
        return {"status": "partial_error", "message": f"Enqueued {len(job_ids)} jobs, {len(errors)} failed", "task_id": str(parent_task.id), **summary}
    invalidate_api_caches("subscriptions", "creators")
    await task_service.update_task(parent_task, status="complete", result=summary, progress={"phase": "complete", "label": "Subscription sync enqueued", "current": len(sub_sources), "total": len(sub_sources)})
    await db.commit()
    return {"status": "ok", "message": f"Enqueued {len(job_ids)} download jobs", "task_id": str(parent_task.id), **summary}


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
    result = await svc.create_subscription(data.model_dump())
    invalidate_creator_subscription_caches()
    return result


@router.patch("/{subscription_id}", response_model=SubscriptionRead)
async def update_subscription(subscription_id: UUID, data: SubscriptionUpdate, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        result = await svc.update_subscription(subscription_id, data.model_dump(exclude_unset=True))
        invalidate_creator_subscription_caches()
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{subscription_id}", status_code=204)
async def delete_subscription(subscription_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        await svc.delete_subscription(subscription_id)
        invalidate_creator_subscription_caches()
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
        result = await svc.add_source(d)
        invalidate_creator_subscription_caches(include_works=True)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{subscription_id}/sources/{ss_id}", response_model=SubscriptionSourceRead)
async def update_subscription_source(
    subscription_id: UUID, ss_id: UUID, data: SubscriptionSourceUpdate, db: AsyncSession = Depends(get_db)
):
    svc = SubscriptionService(db)
    try:
        result = await svc.update_source(ss_id, data.model_dump(exclude_none=True))
        invalidate_creator_subscription_caches(include_works=True)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{subscription_id}/sources/{ss_id}", status_code=204)
async def delete_subscription_source(subscription_id: UUID, ss_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    try:
        await svc.delete_source(ss_id)
        invalidate_creator_subscription_caches(include_works=True)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
