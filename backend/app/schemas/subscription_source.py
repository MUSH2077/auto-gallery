from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SubscriptionSourceCreate(BaseModel):
    subscription_id: UUID | None = None
    source: str
    source_creator_id: str | None = None
    source_url: str | None = None
    is_enabled: bool = True

    model_config = {"from_attributes": True}


class SubscriptionSourceUpdate(BaseModel):
    source_creator_id: str | None = None
    source_url: str | None = None
    is_enabled: bool | None = None

    model_config = {"from_attributes": True}


class SubscriptionSourceRead(BaseModel):
    id: UUID
    subscription_id: UUID
    source: str
    source_creator_id: str | None = None
    source_url: str | None = None
    is_enabled: bool
    last_successful_auth: datetime | None = None
    auth_healthy: bool
    last_synced_at: datetime | None = None
    last_attempted_at: datetime | None = None
    next_sync_at: datetime | None = None
    auth_status: str | None = None
    auth_error_reason: str | None = None
    last_auth_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
