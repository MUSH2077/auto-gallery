from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.subscription import SubscriptionCreate, SubscriptionRead, SubscriptionUpdate
from app.schemas.subscription_source import SubscriptionSourceCreate, SubscriptionSourceRead, SubscriptionSourceUpdate
from app.services.subscription import SubscriptionService

router = APIRouter(dependencies=[RequireAdmin])


@router.get("", response_model=list[SubscriptionRead])
async def list_subscriptions(offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    svc = SubscriptionService(db)
    return await svc.list_subscriptions(offset, limit)


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
