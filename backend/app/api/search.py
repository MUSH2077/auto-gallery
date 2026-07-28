from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.models.user import User
from app.services.search import SearchService

# Reused as-is for both the router-level gate and the route parameter below —
# see app/api/works.py for why this avoids a duplicate permission check/query.
_require_library = RequirePermission("library")

router = APIRouter(dependencies=[_require_library])


@router.get("")
async def search(
    q: str = Query("", description="Search query"),
    kind: str = Query("all", description="Entity type: all, works, creators, tags"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    user: User = _require_library,
    db: AsyncSession = Depends(get_db),
):
    svc = SearchService(db)
    return await svc.search(q, offset, limit, kind=kind, force_sfw=not user.nsfw_visible)
