"""Durable global scheduler admission and batch-item inspection."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from ._routers import router as system_router


class SchedulerSyncNowRequest(BaseModel):
    mode: Literal["force_eligible", "due_scan", "manual_all_enabled"] = "force_eligible"
    request_id: UUID | None = None


class SchedulerSyncAcceptance(BaseModel):
    task_id: UUID
    job_id: str | None
    status: str
    operation_type: Literal["subscription-sync-batch"]
    mode: Literal["force_eligible", "due_scan", "manual_all_enabled"]


class SchedulerBatchItemRead(BaseModel):
    id: UUID
    source_id: UUID
    source: str | None
    status: Literal["pending", "queued", "waiting", "downloading", "importing", "succeeded", "skipped", "failed", "cancelled"]
    attempts: int
    next_retry_at: datetime | None
    download_job_id: UUID | None
    reason_code: str | None
    error: str | None
    outcome: dict | None


class SchedulerBatchItemPage(BaseModel):
    total: int
    items: list[SchedulerBatchItemRead]


@system_router.post("/scheduler/sync-now", status_code=202, response_model=SchedulerSyncAcceptance)
async def trigger_sync_now(data: SchedulerSyncNowRequest | None = None, db: AsyncSession = Depends(get_db), user=RequirePermission("system")):
    """Persist bounded admission; the operations worker owns all source iteration."""
    from app.services.scheduler_batches import admit_batch

    request = data or SchedulerSyncNowRequest()
    return await admit_batch(db, mode=request.mode, request_id=request.request_id, actor_user_id=getattr(user, "id", None))


@system_router.get("/scheduler/batches/{task_id}/items", response_model=SchedulerBatchItemPage)
async def get_scheduler_batch_items(task_id: UUID, offset: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    from app.services.scheduler_batches import batch_items

    return await batch_items(db, task_id, offset=offset, limit=limit)
