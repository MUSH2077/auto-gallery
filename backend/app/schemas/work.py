from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class WorkList(BaseModel):
    id: UUID
    title: str | None = None
    posted_at: str | None = None
    is_nsfw: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkRead(BaseModel):
    id: UUID
    title: str | None = None
    description: str | None = None
    posted_at: str | None = None
    is_nsfw: bool
    thumbnail_asset_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
