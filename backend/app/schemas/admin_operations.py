"""Public contracts for asynchronous administrator operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from app.schemas.task_actions import TaskCapabilities


class AdminOperationAccepted(BaseModel):
    task_id: str
    job_id: str
    status: Literal["enqueued"]
    operation_type: str


class AdminOperationAcceptedMessage(AdminOperationAccepted):
    message: str
    options: dict[str, Any] | None = None


class AdminOperationSnapshot(BaseModel):
    task_id: str
    job_id: str | None = None
    status: Literal["complete"]
    operation_type: str
    progress: dict[str, Any] | None = None
    result: dict[str, Any]
    completed_at: datetime


class AdminOperationCurrent(BaseModel):
    task_id: str
    job_id: str | None = None
    status: Literal[
        "enqueued",
        "running",
        "recovering",
        "paused",
        "failed",
        "stale",
        "cancelled",
    ]
    operation_type: str
    progress: dict[str, Any] | None = None


class AdminOperationSnapshotResponse(BaseModel):
    snapshot: AdminOperationSnapshot | None = None
    current: AdminOperationCurrent | None = None


class AdminOperationRead(TaskCapabilities):
    task_id: str | None = None
    job_id: str | None = None
    status: str
    operation_type: str | None = None
    model_config = {"extra": "allow"}


class AdminOperationPage(BaseModel):
    operations: list[AdminOperationRead]
