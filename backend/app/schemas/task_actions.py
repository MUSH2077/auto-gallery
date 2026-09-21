from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field

TaskAction = Literal["retry", "repeat_sync", "pause", "resume", "cancel", "delete", "acknowledge"]


class TaskCapabilities(BaseModel):
    available_actions: list[TaskAction] = Field(default_factory=list)
    disabled_reasons: dict[TaskAction, str] = Field(default_factory=dict)


class RepeatSyncRequest(BaseModel):
    request_id: UUID


class RepeatSyncAccepted(BaseModel):
    task_id: UUID
    job_id: UUID
    previous_job_id: UUID
    request_id: UUID
    action: Literal["repeat_sync"] = "repeat_sync"
    status: Literal["enqueued"] = "enqueued"


class TaskRead(TaskCapabilities):
    id: UUID
    kind: str
    status: str
    operation_type: str | None = None
    subject_type: str | None = None
    subject_id: UUID | None = None
    title: str | None = None
    parent_task_id: UUID | None = None
    triggering_user_subscription_id: UUID | None = None
    triggering_remote_account_id: UUID | None = None
    resource_state: str | None = None
    resource_reason: str | None = None
    attention_state: str | None = None
    reason_code: str | None = None
    acknowledged_at: str | None = None
    resolved_at: str | None = None
    compactable_at: str | None = None
    queue_name: str | None = None
    rq_job_id: str | None = None
    source: str | None = None
    source_url: str | None = None
    progress_stage: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_data: dict | None = None
    result_data: dict | None = None
    error_log: str | None = None
    meta: dict | None = None
    priority: int = 10
    attempts: int = 0
    enqueued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    events: list[dict] = Field(default_factory=list)
    model_config = {"extra": "allow"}


class TaskPage(BaseModel):
    items: list[TaskRead]
    total: int
    model_config = {"extra": "allow"}


class WorkbenchRecentJob(TaskCapabilities):
    id: UUID
    status: str
    model_config = {"extra": "allow"}


class WorkbenchRecent(BaseModel):
    download_jobs: list[WorkbenchRecentJob]
    import_jobs: list[WorkbenchRecentJob]
    model_config = {"extra": "allow"}


class WorkbenchAttention(BaseModel):
    # ``auth_unhealthy_count`` remains as a compatibility alias for clients
    # that have not yet adopted the more precise actionable name.
    auth_unhealthy_count: int = 0
    auth_actionable_count: int = 0
    auth_disabled_or_unchecked_count: int = 0
    credential_issue_count: int = 0
    failed_download_count: int = 0
    failed_import_count: int = 0
    stale_job_count: int = 0
    low_disk_warning: bool = False
    scheduler_disabled_warning: bool = False
    model_config = {"extra": "allow"}


class WorkbenchSummary(BaseModel):
    recent: WorkbenchRecent
    attention: WorkbenchAttention = Field(default_factory=WorkbenchAttention)
    model_config = {"extra": "allow"}
