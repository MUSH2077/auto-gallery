"""Read and write contracts for private remote-follow persistence."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


RemoteSource = Literal["pixiv", "x", "bilibili"]
Confidence = Literal["high", "medium", "low"]
CandidateState = Literal["pending", "dismissed", "imported", "conflict"]


class UserSubscriptionCreate(BaseModel):
    subscription_id: UUID
    is_enabled: bool = True


class UserSubscriptionUpdate(BaseModel):
    is_enabled: bool | None = None


class UserSubscriptionRead(BaseModel):
    id: UUID
    user_id: int
    subscription_id: UUID
    is_enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserSubscriptionSourceCreate(BaseModel):
    subscription_source_id: UUID
    remote_account_id: UUID | None = None
    is_enabled: bool = True


class UserSubscriptionSourceUpdate(BaseModel):
    remote_account_id: UUID | None = None
    is_enabled: bool | None = None


class UserSubscriptionSourceRead(BaseModel):
    id: UUID
    user_subscription_id: UUID
    subscription_source_id: UUID
    remote_account_id: UUID | None = None
    is_enabled: bool
    last_successful_auth: datetime | None = None
    auth_healthy: bool
    last_synced_at: datetime | None = None
    last_attempted_at: datetime | None = None
    next_sync_at: datetime | None = None
    auth_status: str | None = None
    auth_error_reason: str | None = None
    last_auth_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RemoteAccountCreate(BaseModel):
    source: RemoteSource
    remote_user_id: str | None = Field(default=None, max_length=255)
    remote_username: str | None = Field(default=None, max_length=255)
    is_enabled: bool = True
    scan_interval_hours: int = Field(default=24, ge=1)
    auto_import_enabled: bool = False
    auto_import_min_confidence: Confidence = "high"
    auto_import_limit: int = Field(default=25, ge=1, le=200)


class RemoteAccountUpdate(BaseModel):
    remote_user_id: str | None = Field(default=None, max_length=255)
    remote_username: str | None = Field(default=None, max_length=255)
    is_enabled: bool | None = None
    scan_interval_hours: int | None = Field(default=None, ge=1)
    auto_import_enabled: bool | None = None
    auto_import_min_confidence: Confidence | None = None
    auto_import_limit: int | None = Field(default=None, ge=1, le=200)


class RemoteAccountRead(BaseModel):
    id: UUID
    user_id: int
    source: RemoteSource
    remote_user_id: str | None = None
    remote_username: str | None = None
    is_enabled: bool
    auth_status: str | None = None
    auth_error_reason: str | None = None
    last_authenticated_at: datetime | None = None
    last_scan_started_at: datetime | None = None
    last_scan_completed_at: datetime | None = None
    next_scan_at: datetime | None = None
    scan_interval_hours: int
    auto_import_enabled: bool
    auto_import_min_confidence: Confidence
    auto_import_limit: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DiscoveryCandidateRead(BaseModel):
    id: UUID
    remote_account_id: UUID
    remote_creator_id: str
    remote_url: str | None = None
    display_name: str | None = None
    metadata: dict | None = Field(default=None, validation_alias="candidate_metadata")
    confidence: Confidence
    confidence_reasons: list | None = None
    state: CandidateState
    subscription_id: UUID | None = None
    user_subscription_id: UUID | None = None
    dismissed_at: datetime | None = None
    imported_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
