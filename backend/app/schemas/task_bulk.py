"""Per-item bulk results, including policy refusals and ordinary string errors."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.task_actions import TaskAction, TaskCapabilities


class TaskActionRefusal(TaskCapabilities):
    code: Literal["invalid_task_action"]
    action: TaskAction
    reason: str
    # Final liveness/receipt checks can refuse without an advisory snapshot.
    # Routes exclude unset fields to preserve those existing shorter payloads.
    status: str | None = None
    model_config = {"extra": "allow"}


class TaskBulkError(BaseModel):
    id: UUID
    error: TaskActionRefusal | str


class TaskBulkResult(BaseModel):
    action: Literal["retry", "pause", "resume", "cancel", "delete"]
    task_type: Literal["download", "import"]
    filters: dict[str, Any]
    total_matched: int
    succeeded: int
    failed: int
    errors: list[TaskBulkError]


class TaskBulkStatusResult(TaskBulkResult):
    status: Literal["ok"]


class TaskBulkClearResult(TaskBulkStatusResult):
    deleted: int
