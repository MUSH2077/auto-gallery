from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreatorLinkCreate(BaseModel):
    creator_id: UUID
    url: str
    link_type: str
    source: str | None = None
    confidence: float = 1.0
    is_verified: bool = False
    notes: str | None = None

    model_config = {"from_attributes": True}


class CreatorLinkUpdate(BaseModel):
    url: str | None = None
    link_type: str | None = None
    confidence: float | None = None
    is_verified: bool | None = None
    notes: str | None = None

    model_config = {"from_attributes": True}


class CreatorLinkRead(BaseModel):
    id: UUID
    creator_id: UUID
    url: str
    link_type: str
    source: str | None = None
    confidence: float
    is_verified: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
