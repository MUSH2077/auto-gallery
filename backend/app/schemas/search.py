from __future__ import annotations

from typing import Literal

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
