from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class RepositoryCapabilities(BaseModel):
    can_download: bool
    can_import_local: bool
    supports_gallerydl: bool
    supports_tags: bool
    is_reference_only: bool


class RepositoryProvider(BaseModel):
    source: str
    display_name: str
    normalized_url: str | None = None
    url_valid: bool
    capabilities: RepositoryCapabilities


class RepositorySyncOutcome(BaseModel):
    code: Literal["new_content", "no_changes", "no_content"]
    metadata_count: int | None = None
    media_count: int | None = None
    completed_at: str | None = None
    downloaded_metadata_count: int | None = None
    recovered_metadata_count: int | None = None
    pending_work_count: int | None = None


class RepositoryRecentJob(BaseModel):
    id: str
    subscription_id: str
    subscription_source_id: str | None = None
    source: str
    source_url: str | None = None
    status: str
    retry_count: int
    error_log_excerpt: str | None = None
    outcome: RepositorySyncOutcome | None = None
    created_at: str | None = None
    updated_at: str | None = None
    record_type: str | None = None
    receipt_id: str | None = None
    task_id: str | None = None
    original_task_id: str | None = None
    download_job_id: str | None = None
    import_job_id: str | None = None
    outcome_code: str | None = None
    attempts: int | None = None
    metadata_count: int | None = None
    media_count: int | None = None
    works_imported: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    recovered: bool | None = None
    recovered_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class RepositoryRead(BaseModel):
    id: UUID
    subscription_id: UUID
    source: str
    source_display_name: str
    source_creator_id: str | None = None
    source_url: str | None = None
    is_enabled: bool
    auth_healthy: bool
    auth_status: str | None = None
    auth_error_reason: str | None = None
    last_auth_checked_at: str | None = None
    last_successful_auth: str | None = None
    last_synced_at: str | None = None
    last_attempted_at: str | None = None
    can_download: bool
    supports_gallerydl: bool
    url_valid: bool
    is_repository: bool
    latest_job: RepositoryRecentJob | None = None
    created_at: str | None = None
    updated_at: str | None = None


class RepositoryCreator(BaseModel):
    id: UUID
    name: str
    display_name: str | None = None
    thumbnail_url: str | None = None
    is_favorite: bool


class RepositorySubscription(BaseModel):
    id: UUID
    name: str | None = None
    is_active: bool
    sync_enabled: bool
    sync_interval_hours: int
    schedule_mode: str | None = None
    schedule_rule: dict | None = None
    scheduled_times: str | None = None
    last_synced_at: str | None = None


class RepositoryRecentWork(BaseModel):
    id: str
    title: str | None = None
    posted_at: str | None = None
    thumbnail_asset_id: str | None = None
    asset_count: int
    has_video: bool
    is_nsfw: bool
    is_ai_generated: bool
    is_favorite: bool
    created_at: str | None = None
    source: str
    creator_name: str
    creator_id: str


class RepositoryDetailResponse(BaseModel):
    repository: RepositoryRead
    creator: RepositoryCreator
    subscription: RepositorySubscription
    provider: RepositoryProvider
    recent_jobs: list[RepositoryRecentJob]
    active_jobs: list[RepositoryRecentJob]
    sync_history: list[RepositoryRecentJob]
    work_total: int
    recent_works: list[RepositoryRecentWork]
