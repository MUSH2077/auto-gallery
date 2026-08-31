"""Read-only creator reference groups."""

from typing import Literal

from pydantic import BaseModel, Field


class PixivCreatorReferenceRead(BaseModel):
    source_creator_id: str
    display_name: str
    username: str | None = None
    profile_url: str
    avatar_url: str | None = None
    status: Literal["remote", "fallback"]
    error_code: str | None = None


class DanbooruCreatorReferenceRead(BaseModel):
    artist_id: int
    name: str | None = None
    other_names: list[str] = Field(default_factory=list)
    profile_url: str
    status: Literal["remote", "fallback"]


class CreatorReferencesRead(BaseModel):
    pixiv: list[PixivCreatorReferenceRead] = Field(default_factory=list)
    danbooru: DanbooruCreatorReferenceRead | None = None
