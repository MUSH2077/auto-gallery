from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.curation import CurationStateRead


class WorkList(BaseModel):
    id: UUID
    title: str | None = None
    posted_at: datetime | None = None
    thumbnail_asset_id: UUID | None = None
    thumbnail_width: int | None = Field(default=None, ge=1)
    thumbnail_height: int | None = Field(default=None, ge=1)
    asset_count: int = 1
    is_nsfw: bool
    is_ai_generated: bool = False
    created_at: datetime
    source: str | None = None
    creator_name: str | None = None
    creator_id: str | None = None
    has_ugoira: bool = False
    has_video: bool = False
    preview_asset_ids: list[str] = []
    is_favorite: bool = False
    curation_visibility: str = "visible"

    model_config = {"from_attributes": True}


class WorkListResponse(BaseModel):
    total: int
    items: list[WorkList]


class WorkRead(BaseModel):
    id: UUID
    title: str | None = None
    description: str | None = None
    posted_at: datetime | None = None
    thumbnail_asset_id: UUID | None = None
    asset_count: int = 1
    is_nsfw: bool
    is_ai_generated: bool = False
    is_favorite: bool
    creator_id: UUID | None = None
    creator_name: str | None = None
    curation_state: CurationStateRead | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RemoteWorkStateRead(BaseModel):
    source: Literal["pixiv"]
    source_work_id: str
    fetched_at: datetime
    total_views: int = Field(ge=0)
    total_bookmarks: int = Field(ge=0)
    is_bookmarked: bool
