"""Public contracts for asynchronous administrator operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class AdminOperationAccepted(BaseModel):
    task_id: str
    job_id: str
    status: Literal["enqueued"]
    operation_type: str


class AdminOperationSnapshot(BaseModel):
    task_id: str
    job_id: str | None = None
    status: Literal["complete"]
    operation_type: str
    progress: dict[str, Any] | None = None
    result: dict[str, Any]
    completed_at: datetime


class AdminOperationSnapshotResponse(BaseModel):
    snapshot: AdminOperationSnapshot | None = None
