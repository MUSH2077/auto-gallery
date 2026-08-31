from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAnyPermission
from app.database import get_db
from app.models.user import User
from app.models.remote_discovery import UserSubscription, UserSubscriptionSource
from app.schemas.search import SearchAssistRequest, SearchScopeValue
from app.services.search import SearchBackendUnavailable, SearchPermissionError, SearchService
from app.services.search_language import SCOPE_TARGETS, SearchQueryError

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


@router.get("")
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
        memberships = []
        bindings = []
        user_id = getattr(user, "id", None)
        # A few API-contract tests intentionally replace the database
        # dependency with ``None`` while mocking SearchService.  Real requests
        # always have both a session and authenticated user id; retaining this
        # narrow seam keeps those request-shape tests independent of storage.
        if db is not None and user_id is not None:
            memberships = list(
                (
                    await db.execute(
                        select(UserSubscription).where(UserSubscription.user_id == user_id)
                    )
                ).scalars()
            )
            bindings = list(
                (
                    await db.execute(
                        select(UserSubscriptionSource).where(
                            UserSubscriptionSource.user_id == user_id
                        )
                    )
                ).scalars()
            )
        memberships_by_subscription = {
            membership.subscription_id: membership for membership in memberships
        }
        bindings_by_repository = {
            binding.subscription_source_id: binding for binding in bindings
        }
        result = await svc.search(
            q,
            offset,
            limit,
            scope=scope,
            permissions=_permissions(user),
            force_sfw=not user.nsfw_visible,
            cursor=cursor,
            allowed_subscription_ids=set(memberships_by_subscription),
            allowed_repository_ids=set(bindings_by_repository),
            user_id=user_id,
        )
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
            item.update(
                {
                    "subscription_name": membership.name if membership else None,
                    "is_enabled": binding.is_enabled,
                    "auth_healthy": binding.auth_healthy,
                    "auth_status": binding.auth_status,
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
