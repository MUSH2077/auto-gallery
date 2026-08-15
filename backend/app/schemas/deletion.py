from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


DeletionEntityType = Literal["repository", "subscription", "creator"]
DeletionMode = Literal["soft", "permanent"]


class DeletionPreviewResponse(BaseModel):
    entity_type: DeletionEntityType
    entity_ids: list[UUID]
    mode: DeletionMode
    can_delete_files: bool
    active_task_count: int = 0
    active_job_count: int = 0
    active_task_ids: list[UUID] = Field(default_factory=list)
    affected_work_count: int = 0
    exclusive_work_count: int = 0
    shared_work_count: int = 0
    exclusive_asset_count: int = 0


class DeletionResultResponse(BaseModel):
    status: Literal["soft_deleted", "enqueued"]
    mode: DeletionMode
    entity_type: DeletionEntityType
    entity_ids: list[UUID]
    delete_files: bool = False
    task_id: UUID | None = None
    message: str | None = None


class BatchDeletionRequest(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=100)
    delete_files: bool = False
