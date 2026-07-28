from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SubscriptionCreate(BaseModel):
    creator_id: UUID
    name: str | None = None
    is_active: bool = True
    sync_enabled: bool = True
    sync_interval_hours: int = 6
    schedule_mode: str | None = None
    scheduled_times: str | None = None

    model_config = {"from_attributes": True}


class SubscriptionUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    sync_enabled: bool | None = None
    sync_interval_hours: int | None = None
    schedule_mode: str | None = None
    scheduled_times: str | None = None

    model_config = {"from_attributes": True}


class SubscriptionRead(BaseModel):
    id: UUID
    creator_id: UUID
    name: str | None = None
    creator_name: str | None = None
    creator_display_name: str | None = None
    is_active: bool
    sync_enabled: bool
    sync_interval_hours: int = 6
    schedule_mode: str | None = None
    scheduled_times: str | None = None
    last_synced_at: datetime | None = None
    source_count: int | None = None
    enabled_source_count: int | None = None
    running_job_count: int | None = None
    failed_job_count: int | None = None
    latest_job_id: UUID | None = None
    latest_job_status: str | None = None
    latest_job_created_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
