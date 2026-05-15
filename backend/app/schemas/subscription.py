from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SubscriptionCreate(BaseModel):
    creator_id: UUID
    name: str | None = None
    is_active: bool = True
    sync_enabled: bool = True

    model_config = {"from_attributes": True}


class SubscriptionUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    sync_enabled: bool | None = None

    model_config = {"from_attributes": True}


class SubscriptionRead(BaseModel):
    id: UUID
    creator_id: UUID
    name: str | None = None
    is_active: bool
    sync_enabled: bool
    last_synced_at: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
