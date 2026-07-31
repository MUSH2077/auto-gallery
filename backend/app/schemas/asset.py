from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class AssetRead(BaseModel):
    id: UUID
    file_name: str
    file_size: int | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    thumb_sm_path: str | None = None
    thumb_md_path: str | None = None
    thumb_lg_path: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkAssetRead(BaseModel):
    id: UUID
    file_name: str
    file_path: str
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    mime_type: str | None = None
    media_kind: Literal["image", "animated_image", "video", "archive", "unknown"]
    thumb_sm_path: str | None = None
    thumb_md_path: str | None = None
    thumb_lg_path: str | None = None
    thumb_url: str | None = None
    poster_url: str | None = None
    preview_url: str | None = None
    original_url: str | None = None
    created_at: datetime


class PlaybackTicketRead(BaseModel):
    url: str
    expires_at: datetime
