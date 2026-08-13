from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAnyPermission
from app.database import get_db
from app.models.user import User
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
        return await svc.search(
            q,
            offset,
            limit,
            scope=scope,
            permissions=_permissions(user),
            force_sfw=not user.nsfw_visible,
            cursor=cursor,
        )
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
        return await svc.assist(
            before_cursor=data.before_cursor,
            after_cursor=data.after_cursor,
            scope=data.scope,
            limit=data.limit,
            permissions=_permissions(user),
            compose=data.compose.model_dump() if data.compose else None,
            composes=[item.model_dump() for item in data.composes],
        )
    except SearchQueryError as error:
        _raise_search_error(error)
