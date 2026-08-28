"""Read and write contracts for private remote-follow persistence."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.schedule import CalendarScheduleRule, normalize_legacy_schedule_payload


RemoteSource = Literal["pixiv", "x", "bilibili"]
Confidence = Literal["high", "medium", "low"]
CandidateState = Literal["pending", "dismissed", "imported", "conflict"]
AuthMethod = Literal["refresh_token", "oauth2", "cookie", "sessdata"]
StoredScheduleMode = Literal["interval", "calendar", "manual"]


class _UserSubscriptionScheduleInput(BaseModel):
    schedule_mode: StoredScheduleMode | None = None
    schedule_rule: CalendarScheduleRule | None = None
    scheduled_times: str | None = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def normalize_schedule(cls, value: Any) -> Any:
        value = normalize_legacy_schedule_payload(value)
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        mode_present = "schedule_mode" in payload
        sync_present = "sync_enabled" in payload
        mode = payload.get("schedule_mode")
        if mode == "inherit":
            payload["schedule_mode"] = None
            payload["schedule_rule"] = None
            payload["sync_enabled"] = True
        elif mode == "manual" or (sync_present and payload.get("sync_enabled") is False):
            payload["schedule_mode"] = "manual"
            payload["sync_enabled"] = False
        elif mode_present and mode in {"interval", "calendar"}:
            payload["sync_enabled"] = True
        return payload

    @model_validator(mode="after")
    def require_calendar_rule(self):
        if self.schedule_mode == "calendar" and self.schedule_rule is None:
            raise ValueError("calendar schedule_mode requires schedule_rule")
        return self


class UserSubscriptionCreate(_UserSubscriptionScheduleInput):
    subscription_id: UUID
    name: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    sync_enabled: bool = True
    sync_interval_hours: int = Field(default=6, ge=1)


class UserSubscriptionUpdate(_UserSubscriptionScheduleInput):
    name: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    sync_enabled: bool | None = None
    sync_interval_hours: int | None = Field(default=None, ge=1)


class UserSubscriptionRead(BaseModel):
    id: UUID
    user_id: int
    subscription_id: UUID
    name: str | None = None
    is_active: bool
    sync_enabled: bool
    sync_interval_hours: int
    schedule_mode: StoredScheduleMode | None = None
    schedule_rule: dict | None = None
    scheduled_times: str | None = None
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
    user_id: int
    subscription_id: UUID
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
    auth_method: AuthMethod | None = None
    scopes: list[str] = Field(default_factory=list)
    collection_selectors: list[dict[str, Any]] = Field(default_factory=list)
    is_enabled: bool = True
    scan_interval_hours: int = Field(default=24, ge=1)
    auto_import_enabled: bool = False
    auto_import_min_confidence: Confidence = "high"
    auto_import_limit: int = Field(default=25, ge=1, le=200)
    credentials: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_auth_method_for_source(self):
        allowed = {
            "pixiv": {"refresh_token"},
            "x": {"oauth2", "cookie"},
            "bilibili": {"sessdata"},
        }
        if self.auth_method is not None and self.auth_method not in allowed[self.source]:
            raise ValueError(f"auth_method {self.auth_method!r} is not valid for {self.source}")
        return self


class RemoteAccountUpdate(BaseModel):
    remote_user_id: str | None = Field(default=None, max_length=255)
    remote_username: str | None = Field(default=None, max_length=255)
    auth_method: AuthMethod | None = None
    scopes: list[str] | None = None
    collection_selectors: list[dict[str, Any]] | None = None
    is_enabled: bool | None = None
    scan_interval_hours: int | None = Field(default=None, ge=1)
    auto_import_enabled: bool | None = None
    auto_import_min_confidence: Confidence | None = None
    auto_import_limit: int | None = Field(default=None, ge=1, le=200)
    credentials: dict[str, str] | None = Field(default=None, min_length=1)


class RemoteAccountRead(BaseModel):
    id: UUID
    user_id: int
    source: RemoteSource
    remote_user_id: str | None = None
    remote_username: str | None = None
    auth_method: AuthMethod | None = None
    scopes: list[str] = Field(default_factory=list)
    collection_selectors: list[dict[str, Any]] = Field(default_factory=list)
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
    has_credentials: bool = False
    credential_mask: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DiscoveryCandidateRead(BaseModel):
    id: UUID
    remote_account_id: UUID
    user_id: int
    source_creator_id: str
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
    is_following: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DiscoveryScanCreate(BaseModel):
    remote_account_id: UUID


class DiscoveryCandidateBatchAction(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=200)
    action: Literal["import", "dismiss", "restore"]
    immediate_sync: bool = False


class DiscoveryCandidateResolve(BaseModel):
    creator_id: UUID | None = None
    creator_name: str | None = Field(default=None, min_length=1, max_length=500)
    immediate_sync: bool = False

    @model_validator(mode="after")
    def require_one_resolution_target(self):
        if self.creator_id is not None and self.creator_name is not None:
            raise ValueError("Specify creator_id or creator_name, not both")
        return self
