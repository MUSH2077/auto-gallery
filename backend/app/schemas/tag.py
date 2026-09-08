from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TagCreate(BaseModel):
    normalized_name: str
    category: str | None = None


class TagUpdate(BaseModel):
    normalized_name: str | None = None
    category: str | None = None


class TagSourceUsage(BaseModel):
    source: str
    work_count: int


class TagRead(BaseModel):
    id: UUID
    normalized_name: str
    category: str | None = None
    usage_count: int = 0
    source_usage: list[TagSourceUsage] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class CreatorRef(BaseModel):
    creator_id: UUID
    creator_name: str
    work_count: int


class TagDetail(TagRead):
    top_creators: list[CreatorRef] = []


class TagPage(BaseModel):
    items: list[TagRead]
    total: int
    offset: int
    limit: int
    next_offset: int | None
