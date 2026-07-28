from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin
from app.database import get_db
from app.services.search import SearchService

router = APIRouter(dependencies=[RequireAdmin])


@router.get("")
async def search(
    q: str = Query("", description="Search query"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    svc = SearchService(db)
    return await svc.search(q, offset, limit)
