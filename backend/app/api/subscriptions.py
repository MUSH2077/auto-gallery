import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.auth import RequirePermission
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.schemas.subscription import (
    SubscriptionCreate,
    SubscriptionRead,
    SubscriptionSummariesResponse,
    SubscriptionUpdate,
)
from app.schemas.subscription_source import SubscriptionSourceCreate, SubscriptionSourceRead, SubscriptionSourceUpdate
from app.schemas.deletion import (
    BatchDeletionRequest,
    DeletionPreviewResponse,
    DeletionResultResponse,
)
from app.services.subscription import SubscriptionService, SubscriptionValidationError
from app.services.search import SearchBackendUnavailable, SearchService
from app.services.search_language import SearchQueryError
from app.services.cache import (
    invalidate_api_caches,
    invalidate_creator_subscription_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequirePermission("subscriptions")])



@router.get("/count")
async def count_subscriptions(db: AsyncSession = Depends(get_db)):
    """Return total number of subscriptions."""
    result = await db.execute(select(func.count()).select_from(Subscription))
    return {"count": result.scalar() or 0}


@router.get("", response_model=list[SubscriptionRead])
async def list_subscriptions(
    offset: int = 0, limit: int = 50,
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await SearchService(db).search(
            q,
            offset,
            limit,
            scope="subscriptions",
            permissions={"subscriptions"},
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    except SearchBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "search_unavailable", "message": str(exc)},
        ) from exc
    return [
        SubscriptionRead.model_validate(item)
        for item in result["groups"]["subscriptions"]["items"]
    ]


# ── Batch Operations ──

@router.post("/batch-deletion-preview", response_model=DeletionPreviewResponse)
async def batch_subscription_deletion_preview(
    data: BatchDeletionRequest,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    scope = await HierarchicalDeletionService(db).scope("subscription", data.ids)
    return scope.preview_payload(is_admin=user.is_admin)


@router.post(
    "/batch-delete",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def batch_delete_subscriptions(
    data: BatchDeletionRequest,
    response: Response,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import (
        HierarchicalDeletionService,
        enqueue_permanent_deletion,
    )

    if data.delete_files and not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required to delete files")
    svc = HierarchicalDeletionService(db)
    scope = await svc.scope("subscription", data.ids)
    svc.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "subscription", data.ids, delete_files=data.delete_files
        )
    else:
        result = await svc.soft_delete(scope)
    invalidate_creator_subscription_caches()
    return result


@router.post("/batch-toggle-sync")
async def batch_toggle_sync(data: dict, db: AsyncSession = Depends(get_db)):
    """Enable/disable sync for multiple subscriptions."""
    ids = data.get("ids", [])
    enabled = data.get("sync_enabled", True)
    svc = SubscriptionService(db)
    results = []
    for sid in ids:
        try:
            subscription_id = UUID(sid)
        except (TypeError, ValueError):
            results.append({"id": sid, "status": "error", "error": "invalid_id"})
            continue
        try:
            await svc.update_subscription(subscription_id, {"sync_enabled": enabled})
            results.append({"id": sid, "status": "updated", "sync_enabled": enabled})
        except ValueError:
            results.append({"id": sid, "status": "error", "error": "not_found"})
        except Exception:
            logger.warning("batch subscription update failed for %s", sid, exc_info=True)
            results.append({"id": sid, "status": "error", "error": "internal_error"})
    invalidate_api_caches("subscriptions", "creators")
    return {"status": "ok", "results": results}


@router.get("/summaries", response_model=SubscriptionSummariesResponse)
async def get_subscription_summaries(
    ids: str = Query(..., min_length=36, max_length=1900),
    db: AsyncSession = Depends(get_db),
):
    """Return authoritative operational and schedule state for one list page."""

    raw_ids = [value.strip() for value in ids.split(",") if value.strip()]
    if not raw_ids or len(raw_ids) > 50:
        raise HTTPException(status_code=422, detail="ids must contain between 1 and 50 UUIDs")
    try:
        subscription_ids = [UUID(value) for value in raw_ids]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="ids contains an invalid UUID") from exc
    from app.services.subscription_summary import subscription_summaries

    return await subscription_summaries(db, subscription_ids)


@router.post("/{subscription_id}/sync-now")
async def trigger_subscription_sync(subscription_id: UUID, db: AsyncSession = Depends(get_db)):
    """Manually trigger sync for a single subscription — creates download jobs for all enabled sources."""
    from app.services.subscription import SubscriptionService
    try:
        return await SubscriptionService(db).trigger_sync(subscription_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
    except SubscriptionValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{subscription_id}/deletion-preview", response_model=DeletionPreviewResponse)
async def subscription_deletion_preview(
    subscription_id: UUID,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.hierarchical_deletion import HierarchicalDeletionService

    scope = await HierarchicalDeletionService(db).scope("subscription", [subscription_id])
    return scope.preview_payload(is_admin=user.is_admin)


@router.delete(
    "/{subscription_id}",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def delete_subscription(
    subscription_id: UUID,
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
    scope = await svc.scope("subscription", [subscription_id])
    svc.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "subscription", [subscription_id], delete_files=delete_files
        )
    else:
        result = await svc.soft_delete(scope)
    invalidate_creator_subscription_caches()
    return result


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


@router.delete(
    "/{subscription_id}/sources/{ss_id}",
    response_model=DeletionResultResponse,
    responses={202: {"model": DeletionResultResponse, "description": "Permanent deletion queued"}},
)
async def delete_subscription_source(
    subscription_id: UUID,
    ss_id: UUID,
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
    scope = await svc.scope("repository", [ss_id])
    if subscription_id not in scope.subscription_ids:
        raise HTTPException(status_code=404, detail="Repository not found in subscription")
    svc.ensure_no_active_jobs(scope)
    if user.is_admin:
        response.status_code = 202
        result = await enqueue_permanent_deletion(
            "repository", [ss_id], delete_files=delete_files
        )
    else:
        result = await svc.soft_delete(scope)
    invalidate_creator_subscription_caches(include_works=True)
    return result
