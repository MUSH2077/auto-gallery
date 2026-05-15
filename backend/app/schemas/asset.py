from datetime import datetime
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
