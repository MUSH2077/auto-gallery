from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ImportJobRead(BaseModel):
    id: UUID
    download_job_id: UUID
    status: str
    error_log: str | None = None
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
