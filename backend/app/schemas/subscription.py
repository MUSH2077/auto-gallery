from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, model_validator

from app.schemas.schedule import CalendarScheduleRule, normalize_legacy_schedule_payload


ScheduleMode = Literal["inherit", "interval", "calendar", "manual"]


class _ScheduleInput(BaseModel):
    schedule_mode: ScheduleMode | None = None
    schedule_rule: CalendarScheduleRule | None = None
    scheduled_times: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_schedule(cls, value):
        return normalize_legacy_schedule_payload(value)

    @model_validator(mode="after")
    def require_calendar_rule(self):
        if self.schedule_mode == "calendar" and self.schedule_rule is None:
            raise ValueError("calendar schedule_mode requires schedule_rule")
        return self


class ActivatedSubscriptionSource(BaseModel):
    id: UUID
    source: str
    source_url: str | None = None
    selection_reason: str


class SubscriptionCreate(_ScheduleInput):
    creator_id: UUID
    name: str | None = None
    is_active: bool = True
    sync_enabled: bool = True
    sync_interval_hours: int = 6

    model_config = {"from_attributes": True}


class SubscriptionUpdate(_ScheduleInput):
    name: str | None = None
    is_active: bool | None = None
    sync_enabled: bool | None = None
    sync_interval_hours: int | None = None

    model_config = {"from_attributes": True}


class SubscriptionRead(BaseModel):
    id: UUID
    creator_id: UUID
    name: str | None = None
    creator_name: str | None = None
    creator_display_name: str | None = None
    is_active: bool
    sync_enabled: bool
    sync_interval_hours: int = 6
    schedule_mode: str | None = None
    schedule_rule: dict | None = None
    scheduled_times: str | None = None
    last_synced_at: datetime | None = None
    source_count: int | None = None
    enabled_source_count: int | None = None
    running_job_count: int | None = None
    failed_job_count: int | None = None
    latest_job_id: UUID | None = None
    latest_job_status: str | None = None
    latest_job_created_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    configured_mode: str | None = None
    effective_mode: str | None = None
    auto_enabled_source: ActivatedSubscriptionSource | None = None
    next_sync_at: datetime | None = None

    model_config = {"from_attributes": True}


class SubscriptionLatestState(BaseModel):
    state: str
    status: str | None = None
    occurred_at: datetime | None = None
    outcome_code: str | None = None
    reason_code: str | None = None
    repository_id: UUID | None = None
    task_id: UUID | None = None


class SubscriptionScheduleSummary(BaseModel):
    configured_mode: str
    effective_mode: str
    inherited: bool
    timezone: str
    scheduled_times: str | None = None
    schedule_rule: dict | None = None
    sync_interval_hours: int
    next_due_at: datetime | None = None
    oldest_due_at: datetime | None = None
    due_sources: int = 0
    overdue_sources: int = 0
    blocked_sources: int = 0


class SubscriptionSummary(BaseModel):
    subscription_id: UUID
    latest_state: SubscriptionLatestState
    active_count: int = 0
    attention_count: int = 0
    source_count: int = 0
    enabled_source_count: int = 0
    schedule: SubscriptionScheduleSummary


class SubscriptionSummariesResponse(BaseModel):
    items: list[SubscriptionSummary]
    updated_at: datetime
