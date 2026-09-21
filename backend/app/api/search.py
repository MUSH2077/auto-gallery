from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAnyPermission
from app.database import get_db
from app.models.user import User
from app.models.remote_discovery import UserSubscription, UserSubscriptionSource
from app.schemas.search import (
    ReferenceNameAnchorsRead,
    SearchAssistRequest,
    SearchResponseRead,
    SearchScopeValue,
)
from app.services.search import (
    NameAnchorsUnavailable,
    SearchBackendUnavailable,
    SearchPermissionError,
    SearchService,
)
from app.services.search_language import SCOPE_TARGETS, SearchQueryError
from app.services.auth_health import classify_source_health

_require_search = RequireAnyPermission("library", "curation", "subscriptions", "tasks", "upload")

router = APIRouter()


def _permissions(user: User) -> set[str]:
    if user.is_admin:
        return {"library", "curation", "subscriptions", "tasks", "system", "upload"}
    return set(user.permissions or [])


def _raise_search_error(error: SearchQueryError) -> None:
    raise HTTPException(
        status_code=422,
        detail={
            "code": error.diagnostic.code,
            "message": error.diagnostic.message,
            "diagnostic": error.diagnostic.payload(),
        },
    )


@router.get("/name-anchors", response_model=ReferenceNameAnchorsRead)
async def name_anchors(
    scope: str = Query(..., pattern="^(creators|subscriptions)$"),
    q: str = Query("", description="Structured reference query"),
    user: User = _require_search,
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(user, "id", None)
    try:
        return await SearchService(db).name_anchors(
            scope=scope,
            query=q,
            permissions=_permissions(user),
            user_id=user_id,
        )
    except NameAnchorsUnavailable as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "name_anchors_unavailable",
                "message": str(error),
            },
        ) from error
    except SearchQueryError as error:
        _raise_search_error(error)
    except SearchPermissionError as error:
        raise HTTPException(
            status_code=403,
            detail={"code": "permission_denied", "message": str(error)},
        ) from error


@router.get("", response_model=SearchResponseRead)
async def search(
    q: str = Query("", description="Search query"),
    scope: SearchScopeValue = Query("global", description="Search surface and result type"),
    kind: str | None = Query(None, description="Deprecated entity type adapter"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(
        None,
        description="Optional seek cursor for adjacent structured work pages",
    ),
    user: User = _require_search,
    db: AsyncSession = Depends(get_db),
):
    if kind and kind != "all":
        legacy_scope = {
            "works": "works",
            "creators": "creators",
            "tags": "tags",
            "repositories": "repositories",
            "subscriptions": "subscriptions",
        }.get(kind)
        if legacy_scope is None:
            raise HTTPException(status_code=422, detail={"code": "invalid_scope", "message": f"Unknown search kind: {kind}"})
        scope = legacy_scope
    if scope not in SCOPE_TARGETS:
        raise HTTPException(status_code=422, detail={"code": "invalid_scope", "message": f"Unknown search scope: {scope}"})
    svc = SearchService(db)
    try:
        bindings = []
        user_id = getattr(user, "id", None)
        if db is not None and user_id is not None and scope in {"global", "repositories"}:
            bindings = list((await db.execute(
                select(UserSubscriptionSource).where(UserSubscriptionSource.user_id == user_id)
            )).scalars())
        bindings_by_repository = {binding.subscription_source_id: binding for binding in bindings}
        result = await svc.search(
            q,
            offset,
            limit,
            scope=scope,
            permissions=_permissions(user),
            force_sfw=not user.nsfw_visible,
            cursor=cursor,
            allowed_repository_ids=set(bindings_by_repository),
            user_id=user_id,
        )
        # Hydrate only returned pages and owned repository labels. Full-text
        # membership count/offset are already actor-filtered inside Meili.
        subscription_group = result.get("groups", {}).get("subscriptions", {})
        subscription_items = subscription_group.get("items", [])
        returned_ids = {UUID(str(item["id"])) for item in subscription_items if isinstance(item, dict) and item.get("id")}
        for item in result.get("groups", {}).get("repositories", {}).get("items", []):
            if isinstance(item, dict) and item.get("id"):
                binding = bindings_by_repository.get(UUID(str(item["id"])))
                if binding is not None:
                    returned_ids.add(binding.subscription_id)
        memberships_by_subscription = {}
        if returned_ids and db is not None and user_id is not None:
            memberships_by_subscription = {member.subscription_id: member for member in (await db.execute(
                select(UserSubscription).where(UserSubscription.user_id == user_id, UserSubscription.subscription_id.in_(returned_ids))
            )).scalars()}
        if db is not None and user_id is not None:
            subscription_group["items"] = [item for item in subscription_items
                                           if isinstance(item, dict) and item.get("id")
                                           and UUID(str(item["id"])) in memberships_by_subscription]
            removed = len(subscription_items) - len(subscription_group["items"])
            subscription_group["total"] = max(0, subscription_group.get("total", 0) - removed)
            result["subscriptions"] = subscription_group["items"]
            if scope == "subscriptions":
                result["total"] = subscription_group["total"]
        for item in result.get("groups", {}).get("subscriptions", {}).get("items", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            membership = memberships_by_subscription.get(UUID(str(item["id"])))
            if membership is None:
                continue
            item.update(
                {
                    "name": membership.name,
                    "is_active": membership.is_active,
                    "sync_enabled": membership.sync_enabled,
                    "sync_interval_hours": membership.sync_interval_hours,
                    "schedule_mode": membership.schedule_mode,
                }
            )
        for item in result.get("groups", {}).get("repositories", {}).get("items", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            binding = bindings_by_repository.get(UUID(str(item["id"])))
            if binding is None:
                continue
            membership = memberships_by_subscription.get(binding.subscription_id)
            health = (
                classify_source_health(binding, membership)
                if membership is not None
                else None
            )
            item.update(
                {
                    "subscription_name": membership.name if membership else None,
                    "is_enabled": binding.is_enabled,
                    "auth_healthy": binding.auth_healthy,
                    "auth_status": binding.auth_status,
                    "auth_state": health.auth_state if health else "unknown",
                    "credential_state": (
                        health.credential_state if health else "unknown"
                    ),
                    "last_synced_at": (
                        binding.last_synced_at.isoformat()
                        if binding.last_synced_at
                        else None
                    ),
                }
            )
        return result
    except SearchQueryError as error:
        _raise_search_error(error)
    except SearchPermissionError as error:
        raise HTTPException(status_code=403, detail={"code": "permission_denied", "message": str(error)}) from error
    except SearchBackendUnavailable as error:
        raise HTTPException(status_code=503, detail={"code": "search_unavailable", "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_cursor", "message": str(error)},
        ) from error


@router.post("/assist")
async def assist(
    data: SearchAssistRequest,
    user: User = _require_search,
    db: AsyncSession = Depends(get_db),
):
    svc = SearchService(db)
    try:
        allowed_repository_ids = None
        user_id = getattr(user, "id", None)
        if db is not None and user_id is not None:
            allowed_repository_ids = set(
                (
                    await db.execute(
                        select(UserSubscriptionSource.subscription_source_id).where(
                            UserSubscriptionSource.user_id == user_id
                        )
                    )
                ).scalars()
            )
        return await svc.assist(
            before_cursor=data.before_cursor,
            after_cursor=data.after_cursor,
            scope=data.scope,
            limit=data.limit,
            permissions=_permissions(user),
            compose=data.compose.model_dump() if data.compose else None,
            composes=[item.model_dump() for item in data.composes],
            allowed_repository_ids=allowed_repository_ids,
        )
    except SearchQueryError as error:
        _raise_search_error(error)
