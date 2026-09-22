from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


SearchScopeValue = Literal[
    "global",
    "works",
    "creators",
    "tags",
    "repositories",
    "subscriptions",
    "tasks",
    "scheduler",
    "creator-picker",
]


class SearchComposeRequest(BaseModel):
    key: str
    value: str | None = None
    operation: Literal["set", "add", "toggle", "remove", "replace-group"] = "set"
    negated: bool = False
    replace_values: list[str] = Field(default_factory=list)


class SearchAssistRequest(BaseModel):
    before_cursor: str = ""
    after_cursor: str = ""
    scope: SearchScopeValue = "global"
    limit: int = Field(default=10, ge=1, le=30)
    compose: SearchComposeRequest | None = None
    composes: list[SearchComposeRequest] = Field(default_factory=list)


class ReferenceNameAnchorRead(BaseModel):
    key: str
    label: str
    kind: Literal["latin", "digit", "kana", "han", "other"]
    offset: int | None = None
    count: int = Field(ge=0)


class ReferenceNameAnchorsRead(BaseModel):
    scope: Literal["creators", "subscriptions"]
    direction: Literal["asc", "desc"]
    total: int = Field(ge=0)
    items: list[ReferenceNameAnchorRead]


class MatchedCreatorIdentityRead(BaseModel):
    creator_id: UUID
    value: str
    source: str
    kind: str
    is_current: bool
    match_type: Literal["exact", "prefix", "fuzzy"]


class SearchResultRead(BaseModel):
    matched_identity: MatchedCreatorIdentityRead | None = None

    model_config = {"extra": "allow"}


class SearchGroupRead(BaseModel):
    total: int = 0
    items: list[SearchResultRead] = Field(default_factory=list)

    model_config = {"extra": "allow"}


class SearchResponseRead(BaseModel):
    query: str = ""
    canonical_query: str = ""
    seed: int | None = Field(default=None, ge=0, le=4_294_967_295)
    groups: dict[str, SearchGroupRead] = Field(default_factory=dict)
    total: int = 0

    model_config = {"extra": "allow"}
