"""Unified operator-facing task and anomaly feed."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.services.operation_attention import operations_overview


router = APIRouter(dependencies=[RequirePermission("tasks")])


@router.get("/overview")
async def get_operations_overview(
    view: str = Query("attention", pattern="^(attention|active|resolved)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await operations_overview(
            db,
            view=view,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
