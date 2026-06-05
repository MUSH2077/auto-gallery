from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DownloadJobCreate(BaseModel):
    subscription_id: UUID
    subscription_source_id: UUID | None = None
    source: str
    source_url: str

    model_config = {"from_attributes": True}


class DownloadJobRead(BaseModel):
    id: UUID
    subscription_id: UUID
    subscription_source_id: UUID | None = None
    creator_id: UUID | None = None
    creator_name: str | None = None
    subscription_name: str | None = None
    source: str
    source_url: str
    status: str
    retry_count: int
    error_log: str | None = None
    gallerydl_config_path: str | None = None
    download_dir: str | None = None
    manifest: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
