"""Unified operator-facing task and anomaly feed."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.schemas.operation_attention import OperationsOverview
from app.services.operation_attention import operations_overview
from app.services.operations import inaccessible_admin_operation_types
from app.services.tasks import can_access_global_subscription_batch


_require_tasks = RequirePermission("tasks")
router = APIRouter(dependencies=[_require_tasks])


@router.get("/overview", response_model=OperationsOverview)
async def get_operations_overview(
    view: str = Query("attention", pattern="^(attention|active|resolved)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user=_require_tasks,
):
    try:
        return await operations_overview(
            db,
            view=view,
            offset=offset,
            limit=limit,
            excluded_admin_operation_types=inaccessible_admin_operation_types(user),
            user_id=user.id,
            include_global_system_tasks=can_access_global_subscription_batch(user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
