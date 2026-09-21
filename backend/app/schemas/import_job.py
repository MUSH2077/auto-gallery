from app.schemas.task_actions import TaskCapabilities
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ImportJobRead(TaskCapabilities):
    id: UUID
    download_job_id: UUID
    status: str
    error_log: str | None = None
    source: str | None = None
    source_url: str | None = None
    subscription_id: UUID | None = None
    subscription_name: str | None = None
    creator_id: UUID | None = None
    creator_name: str | None = None
    created_at: datetime
    updated_at: datetime
    # Task Engine fields
    priority: int = 10
    user_note: str | None = None
    operator_name: str | None = None
    operator_action: str | None = None
    last_heartbeat_at: datetime | None = None
    worker_pid: int | None = None
    import_retry_count: int = 0
    max_import_retries: int = 3
    progress_stage: str | None = None
    progress_works_done: int | None = None
    progress_works_total: int | None = None
    progress_data: dict | None = None

    model_config = {"from_attributes": True}


class ImportJobPage(BaseModel):
    total: int
    items: list[ImportJobRead]
