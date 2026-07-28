from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AssetDedupAssetRead(BaseModel):
    id: UUID
    file_name: str
    file_size: int | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    phash: str | None = None
    source: str | None = None
    source_work_id: str | None = None
    source_url: str | None = None
    work_id: UUID | None = None
    work_title: str | None = None
    creator_id: UUID | None = None
    creator_name: str | None = None
    posted_at: datetime | None = None
    thumb_url: str
    preview_url: str
    group_id: UUID | None = None
    is_representative: bool = False


class AssetDedupEvidenceRead(BaseModel):
    id: UUID
    algorithm_version: str
    sha256_equal: bool
    phash_distance: int | None = None
    ssim_score: float | None = None
    aspect_ratio_delta: float | None = None
    visual_score: float
    metadata_score: float
    total_score: float
    hard_gate_passed: bool
    facts: dict = Field(default_factory=dict)


class AssetDedupCaseRead(BaseModel):
    id: UUID
    status: str
    revision: int
    left: AssetDedupAssetRead
    right: AssetDedupAssetRead
    evidence: AssetDedupEvidenceRead
    suggested_representative_asset_id: UUID | None = None
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class AssetDedupCasePage(BaseModel):
    items: list[AssetDedupCaseRead]
    total: int
    offset: int
    limit: int


class AssetDedupDecisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    action: Literal["merge", "separate", "defer"]
    representative_asset_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=255)


class AssetDedupDecisionRead(BaseModel):
    decision_id: UUID
    case_id: UUID
    action: str
    status: str
    revision: int
    representative_asset_id: UUID | None = None
    group_id: UUID | None = None
    storage_actions: int = 0
    bytes_reclaimable: int = 0
    curation_commit_id: UUID | None = None


class AssetDedupScanRequest(BaseModel):
    auto_apply: bool = True
    batch_size: int = Field(default=100, ge=10, le=500)


class AssetDedupScanRead(BaseModel):
    scan_id: UUID
    status: str
    assets_scanned: int = 0
    candidates_evaluated: int = 0
    cases_created: int = 0
    assets_grouped: int = 0
    bytes_reclaimable: int = 0
    error: str | None = None
