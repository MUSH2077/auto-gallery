from datetime import datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel


class SchedulerDecisionItem(BaseModel):
    subscription_id: UUID
    subscription_name: str | None
    subscription_active: bool
    subscription_sync_enabled: bool
    creator_id: UUID
    creator_name: str | None
    source_id: UUID
    source: str
    source_display_name: str
    source_url: str | None
    source_creator_id: str | None
    source_enabled: bool
    effective_mode: str
    timezone: str
    scheduled_times: str
    schedule_rule: dict[str, Any] | None
    sync_interval_hours: int | None
    last_synced_at: datetime | None
    last_attempted_at: datetime | None
    due: bool
    decision: str
    reason: str
    suppression_reason: str | None
    next_due_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    auth_healthy: bool
    auth_state: Literal["healthy", "unhealthy", "unknown"]
    credential_state: Literal["ready", "missing", "not_required", "unknown"]
    url_valid: bool
    can_download: bool
    is_overdue: bool
    is_attention: bool


class SchedulerDecisionSummary(BaseModel):
    blocked_count: int
    overdue_count: int
    oldest_overdue_at: datetime | None


class SchedulerDecisionPage(BaseModel):
    updated_at: datetime
    scheduler_enabled: bool
    suppressed_count: int
    timezone: str
    view: Literal["all", "attention"]
    total: int
    items: list[SchedulerDecisionItem]
    offset: int
    limit: int
    next_offset: int | None
    summary: SchedulerDecisionSummary
