"""Mixed attention feed: executable task controls and repository navigation."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.task_actions import TaskCapabilities, TaskRead

NavigationAction = Literal["open_repository", "copy_diagnostics"]


class AttentionItem(BaseModel):
    id: str
    severity: Literal["critical", "warning"]
    status: str
    reason_code: str | None
    title: str
    summary: str | None
    repository_id: UUID | None
    task_id: UUID | None
    occurred_at: str
    source: str | None


class TaskAttentionItem(AttentionItem, TaskCapabilities):
    type: Literal["task"]
    task_id: UUID
    navigation_actions: list[NavigationAction]
    task: TaskRead


class RepositoryAttentionItem(AttentionItem):
    type: Literal["repository"]
    repository_id: UUID
    task_id: None
    available_actions: list[NavigationAction]
    task: None


class OperationsSummary(BaseModel):
    attention: int
    critical: int
    warning: int
    resolved: int
    active: int
    resource_limited: int


class OperationsOverview(BaseModel):
    view: Literal["attention", "active", "resolved"]
    total: int
    summary: OperationsSummary
    items: list[Annotated[TaskAttentionItem | RepositoryAttentionItem, Field(discriminator="type")]]
