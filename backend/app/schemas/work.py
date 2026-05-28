from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class WorkList(BaseModel):
    id: UUID
    title: str | None = None
    posted_at: str | None = None
    thumbnail_asset_id: str | None = None
    asset_count: int = 1
    is_nsfw: bool
    is_ai_generated: bool = False
    created_at: datetime
    source: str | None = None
    creator_name: str | None = None
    creator_id: str | None = None
    has_ugoira: bool = False
    preview_asset_ids: list[str] = []
    is_favorite: bool = False

    model_config = {"from_attributes": True}


class WorkRead(BaseModel):
    id: UUID
    title: str | None = None
    description: str | None = None
    posted_at: str | None = None
    thumbnail_asset_id: str | None = None
    asset_count: int = 1
    is_nsfw: bool
    is_ai_generated: bool = False
    is_favorite: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
