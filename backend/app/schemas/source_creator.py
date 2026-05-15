from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SourceCreatorCreate(BaseModel):
    creator_id: UUID | None = None
    source: str
    source_creator_id: str
    source_url: str | None = None
    display_name: str | None = None
    raw_metadata: dict | None = None

    model_config = {"from_attributes": True}


class SourceCreatorRead(BaseModel):
    id: UUID
    creator_id: UUID | None = None
    source: str
    source_creator_id: str
    source_url: str | None = None
    display_name: str | None = None
    raw_metadata: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
