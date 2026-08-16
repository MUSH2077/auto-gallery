from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CurationStateRead(BaseModel):
    visibility: str = "visible"
    reason: str | None = None
    trashed_at: datetime | None = None
    purged_at: datetime | None = None
    archived_at: datetime | None = None


class CurationChangeRead(BaseModel):
    id: UUID
    commit_id: UUID
    subject_type: str
    subject_id: str
    action: str
    sequence: int | None = None
    before_state: dict | None = None
    after_state: dict | None = None
    diff: dict | None = None
    impact: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CurationCommitRead(BaseModel):
    id: UUID
    parent_commit_id: UUID | None = None
    actor_type: str
    actor_id: str | None = None
    message: str
    trigger: str
    dedupe_key: str | None = None
    occurred_at: datetime
    reverts_commit_id: UUID | None = None
    status: str
    stats: dict | None = None
    metadata: dict | None = None
    is_baseline: bool = False
    revertible: bool = True
    created_at: datetime
    updated_at: datetime
    changes: list[CurationChangeRead] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class CurationCommitListResponse(BaseModel):
    items: list[CurationCommitRead]
    total: int


class CurationRevertResponse(BaseModel):
    status: str
    commit: CurationCommitRead | None = None
    reverted: int = 0
    skipped: int = 0
    conflicts: list[dict] = Field(default_factory=list)


class BatchCurateRequest(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=25)
    action: str = "trash"
    reason: str | None = None
    message: str | None = None


class CreatorCurationRequest(BaseModel):
    action: str
    reason: str | None = None
    message: str | None = None


class PurgePreviewRequest(BaseModel):
    work_ids: list[UUID] | None = Field(default=None, max_length=25)


class PurgePreviewResponse(BaseModel):
    work_count: int
    asset_count: int
    bytes_reclaimable: int
    works: list[dict] = Field(default_factory=list)
    assets: list[dict] = Field(default_factory=list)


class PurgeRequest(BaseModel):
    work_ids: list[UUID] = Field(min_length=1, max_length=25)
    message: str | None = None


class RuleSuggestionRead(BaseModel):
    id: str
    title: str
    description: str
    confidence: float
    impact: dict


class CurationBackfillStatusResponse(BaseModel):
    is_complete: bool
    expected: dict
    existing: dict
    missing: dict


class CurationBackfillRunResponse(BaseModel):
    status: str
    created: dict
    skipped: dict
    expected: dict


class RepositoryGraphNode(BaseModel):
    id: UUID
    message: str
    trigger: str
    occurred_at: datetime
    status: str
    is_baseline: bool = False
    stats: dict | None = None
    thumbnails: list[str] = Field(default_factory=list)
    changes_summary: list[dict] = Field(default_factory=list)


class RepositoryGraphEdge(BaseModel):
    from_id: UUID
    to_id: UUID


class RepositoryGraphResponse(BaseModel):
    repository_id: UUID
    nodes: list[RepositoryGraphNode]
    edges: list[RepositoryGraphEdge]
    total: int
    offset: int
    limit: int
