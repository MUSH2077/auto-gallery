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

    model_config = {"from_attributes": True}
