from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, computed_field

from app.services.sync_outcome import sync_outcome_from_manifest


class DownloadJobCreate(BaseModel):
    subscription_id: UUID
    subscription_source_id: UUID | None = None
    source: str
    source_url: str

    model_config = {"from_attributes": True}


class SyncOutcomeRead(BaseModel):
    code: Literal["new_content", "no_changes", "no_content"]
    metadata_count: int
    media_count: int
    completed_at: datetime


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
    # Task Engine fields
    priority: int = 10
    user_note: str | None = None
    operator_name: str | None = None
    operator_action: str | None = None
    last_heartbeat_at: datetime | None = None
    worker_pid: int | None = None
    pipeline_stage: str | None = None
    progress_data: dict | None = None

    @computed_field
    @property
    def outcome(self) -> SyncOutcomeRead | None:
        value = sync_outcome_from_manifest(self.manifest)
        return SyncOutcomeRead(**value) if value else None

    model_config = {"from_attributes": True}
