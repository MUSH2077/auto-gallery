import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.auth import RequirePermission
from sqlalchemy import select
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
from app.services.subscription_membership import SubscriptionMembershipService
from app.services.search import SearchBackendUnavailable, SearchService
from app.services.search_language import SearchQueryError
from app.services.cache import (
    invalidate_api_caches,
    invalidate_creator_subscription_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequirePermission("subscriptions")])



@router.get("/count")
async def count_subscriptions(
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    """Return total number of subscriptions."""
    return {"count": await SubscriptionMembershipService(db, user.id).count()}


@router.get("", response_model=list[SubscriptionRead])
async def list_subscriptions(
    offset: int = 0, limit: int = 50,
    q: str = "",
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    membership_service = SubscriptionMembershipService(db, user.id)
    try:
        result = await SearchService(db).search(
            q,
            offset,
            limit,
            scope="subscriptions",
            permissions={"subscriptions"},
            user_id=user.id,
        )
    except SearchQueryError as exc:
        raise HTTPException(status_code=422, detail=exc.diagnostic.payload()) from exc
    except SearchBackendUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "search_unavailable", "message": str(exc)},
        ) from exc
    candidate_ids = [
        UUID(str(item.get("id") if isinstance(item, dict) else item.id))
        for item in result["groups"]["subscriptions"]["items"]
        if (item.get("id") if isinstance(item, dict) else getattr(item, "id", None))
    ]
    matched_identities = {
        UUID(str(item["id"])): item.get("matched_identity")
        for item in result["groups"]["subscriptions"]["items"]
        if isinstance(item, dict) and item.get("id") and item.get("matched_identity")
    }
    owned = []
    for subscription_id in candidate_ids:
        try:
            item = await membership_service.get(subscription_id)
            if subscription_id in matched_identities:
                setattr(
                    item,
                    "matched_identity",
                    matched_identities[subscription_id],
                )
            owned.append(item)
        except ValueError:
            continue
    return owned[:limit]


# ── Batch Operations ──

@router.post("/batch-deletion-preview", response_model=DeletionPreviewResponse)
async def batch_subscription_deletion_preview(
    data: BatchDeletionRequest,
    user=RequirePermission("subscriptions"),
    db: AsyncSession = Depends(get_db),
):
    service = SubscriptionMembershipService(db, user.id)
    owned = []
    for subscription_id in data.ids:
        try:
            await service.require_membership(subscription_id)
            owned.append(subscription_id)
        except ValueError:
            continue
    return DeletionPreviewResponse(
        entity_type="subscription",
        entity_ids=owned,
        mode="soft",
        can_delete_files=False,
    )


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
    if data.delete_files:
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Administrator access required to delete files",
            )
        raise HTTPException(status_code=400, detail="Member removal cannot delete shared files")
    svc = SubscriptionMembershipService(db, user.id)
    removed = []
    for subscription_id in data.ids:
        try:
            await svc.remove(subscription_id)
            removed.append(subscription_id)
        except ValueError:
            continue
    await db.commit()
    result = DeletionResultResponse(
        status="soft_deleted",
        mode="soft",
        entity_type="subscription",
        entity_ids=removed,
        delete_files=False,
    )
    invalidate_creator_subscription_caches()
    return result


@router.post("/batch-toggle-sync")
async def batch_toggle_sync(
    data: dict,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    """Enable/disable sync for multiple subscriptions."""
    ids = data.get("ids", [])
    enabled = data.get("sync_enabled", True)
    svc = SubscriptionMembershipService(db, user.id)
    results = []
    for sid in ids:
        try:
            subscription_id = UUID(sid)
        except (TypeError, ValueError):
            results.append({"id": sid, "status": "error", "error": "invalid_id"})
            continue
        try:
            await svc.update(subscription_id, {"sync_enabled": enabled})
            results.append({"id": sid, "status": "updated", "sync_enabled": enabled})
        except ValueError:
            results.append({"id": sid, "status": "error", "error": "not_found"})
        except Exception:
            logger.warning("batch subscription update failed for %s", sid, exc_info=True)
            results.append({"id": sid, "status": "error", "error": "internal_error"})
    await db.commit()
    invalidate_api_caches("subscriptions", "creators")
    return {"status": "ok", "results": results}


@router.get("/summaries", response_model=SubscriptionSummariesResponse)
async def get_subscription_summaries(
    ids: str = Query(..., min_length=36, max_length=1900),
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
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

    membership_service = SubscriptionMembershipService(db, user.id)
    owned_ids = []
    for subscription_id in subscription_ids:
        try:
            await membership_service.require_membership(subscription_id)
            owned_ids.append(subscription_id)
        except ValueError:
            continue
    return await subscription_summaries(db, owned_ids, user_id=user.id)


@router.post("/{subscription_id}/sync-now")
async def trigger_subscription_sync(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    """Manually trigger sync for a single subscription — creates download jobs for all enabled sources."""
    from app.services.subscription import SubscriptionService
    try:
        await SubscriptionMembershipService(db, user.id).require_membership(subscription_id)
        return await SubscriptionService(db).trigger_sync(subscription_id, user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{subscription_id}", response_model=SubscriptionRead)
async def get_subscription(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    svc = SubscriptionMembershipService(db, user.id)
    try:
        return await svc.get(subscription_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", response_model=SubscriptionRead, status_code=201)
async def create_subscription(
    data: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    svc = SubscriptionMembershipService(db, user.id)
    try:
        result = await svc.create_or_join(data.model_dump())
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_creator_subscription_caches()
    return result


@router.patch("/{subscription_id}", response_model=SubscriptionRead)
async def update_subscription(
    subscription_id: UUID,
    data: SubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    svc = SubscriptionMembershipService(db, user.id)
    try:
        result = await svc.update(subscription_id, data.model_dump(exclude_unset=True))
        await db.commit()
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
    try:
        await SubscriptionMembershipService(db, user.id).require_membership(subscription_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return DeletionPreviewResponse(
        entity_type="subscription",
        entity_ids=[subscription_id],
        mode="soft",
        can_delete_files=False,
    )


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
    if delete_files:
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Administrator access required to delete files",
            )
        raise HTTPException(status_code=400, detail="Member removal cannot delete shared files")
    svc = SubscriptionMembershipService(db, user.id)
    try:
        await svc.remove(subscription_id)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = DeletionResultResponse(
        status="soft_deleted",
        mode="soft",
        entity_type="subscription",
        entity_ids=[subscription_id],
        delete_files=False,
    )
    invalidate_creator_subscription_caches()
    return result


@router.get("/{subscription_id}/sources", response_model=list[SubscriptionSourceRead])
async def list_subscription_sources(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    svc = SubscriptionMembershipService(db, user.id)
    try:
        return await svc.list_sources(subscription_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{subscription_id}/sources", response_model=SubscriptionSourceRead, status_code=201)
async def add_subscription_source(
    subscription_id: UUID,
    data: SubscriptionSourceCreate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    svc = SubscriptionMembershipService(db, user.id)
    d = data.model_dump()
    d["subscription_id"] = subscription_id
    try:
        result = await svc.add_or_bind_source(subscription_id, d)
        await db.commit()
        invalidate_creator_subscription_caches(include_works=True)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{subscription_id}/sources/{ss_id}", response_model=SubscriptionSourceRead)
async def update_subscription_source(
    subscription_id: UUID,
    ss_id: UUID,
    data: SubscriptionSourceUpdate,
    db: AsyncSession = Depends(get_db),
    user=RequirePermission("subscriptions"),
):
    if data.source_creator_id is not None or data.source_url is not None:
        raise HTTPException(status_code=422, detail="Shared source identity cannot be edited from a membership")
    svc = SubscriptionMembershipService(db, user.id)
    try:
        result = await svc.update_source(subscription_id, ss_id, data.model_dump(exclude_unset=True))
        await db.commit()
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
    if delete_files:
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Administrator access required to delete files",
            )
        raise HTTPException(status_code=400, detail="Member removal cannot delete shared files")
    svc = SubscriptionMembershipService(db, user.id)
    try:
        await svc.remove_source(subscription_id, ss_id)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = DeletionResultResponse(
        status="soft_deleted",
        mode="soft",
        entity_type="repository",
        entity_ids=[ss_id],
        delete_files=False,
    )
    invalidate_creator_subscription_caches(include_works=True)
    return result
