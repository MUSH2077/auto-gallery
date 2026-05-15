from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreatorCreate(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    is_active: bool = True

    model_config = {"from_attributes": True}


class CreatorUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    is_active: bool | None = None

    model_config = {"from_attributes": True}


class CreatorRead(BaseModel):
    id: UUID
    name: str
    display_name: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
