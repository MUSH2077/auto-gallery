from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class GitlleryRepoStatus(BaseModel):
    repository_id: str
    source: str
    creator_dir: str
    exists: bool
    behind: int
    object_integrity_ok: bool
    drift: list[str] = Field(default_factory=list)
    clean: bool
    product_version: str = "v1"
    format_id: str = "gitllery-segment"
    format_revision: int = 1
    projection_mode: str | None = None
    head_segment: str | None = None
    last_complete_commit_id: str | None = None


class GitlleryStatusResponse(BaseModel):
    repositories: list[GitlleryRepoStatus]
    missing_repos: int
    behind_total: int
    deep: bool = False
    # True when the projection checkpoint is absent (fresh redis, post-rebuild):
    # pending counts are unknown and the queued reconcile job must run. status
    # deliberately has no inline full-history fallback (OOM vector).
    needs_reconcile: bool = False
    product_version: str = "v1"
    format_id: str = "gitllery-segment"
    format_revision: int = 1
    projection_mode: str = "shadow"


class GitlleryCapability(BaseModel):
    enabled: bool
    reason: str | None = None


class GitlleryCapabilities(BaseModel):
    automatic_projection: GitlleryCapability
    reconcile: GitlleryCapability
    backfill: GitlleryCapability
    rebuild: GitlleryCapability
    push: GitlleryCapability
    pull: GitlleryCapability
    verify: GitlleryCapability
    commit: GitlleryCapability


class GitlleryCliSettings(BaseModel):
    max_works_per_commit: Literal[25] = 25
    max_operations_per_commit: Literal[100] = 100
    token_storage: Literal["client_only"] = "client_only"
    server_stores_cli_token: Literal[False] = False
    examples: dict[str, str]


class GitlleryGovernanceScope(BaseModel):
    observation: Literal["host_and_auto_gallery"] = "host_and_auto_gallery"
    enforcement: Literal["auto_gallery_only"] = "auto_gallery_only"
    modifies_other_projects: Literal[False] = False
    modifies_host_configuration: Literal[False] = False


class GitllerySettingsResponse(BaseModel):
    product_name: Literal["Gitllery"] = "Gitllery"
    product_version: Literal["v1"] = "v1"
    format_id: Literal["gitllery-segment"] = "gitllery-segment"
    format_revision: Literal[1] = 1
    projection_mode: Literal["shadow", "active"]
    build_generation: str
    managed_by: Literal["deployment_environment"] = "deployment_environment"
    read_only: Literal[True] = True
    capabilities: GitlleryCapabilities
    cli: GitlleryCliSettings
    governance_scope: GitlleryGovernanceScope
    status: GitlleryStatusResponse


class GitlleryReconcileResponse(BaseModel):
    projected: dict[str, int]
    status: GitlleryStatusResponse


class GitlleryLogEntry(BaseModel):
    commit: str
    db_commit_id: str | None = None
    message: str
    trigger: str | None = None
    actor: str | None = None
    occurred_at: str | None = None
    change_count: int = 0


class GitlleryLogResponse(BaseModel):
    repository_id: str
    entries: list[GitlleryLogEntry]
    total: int


class GitlleryRebuildResponse(BaseModel):
    commits_restored: int = 0
    commits_skipped_auto: int = 0
    commits_deduped: int = 0
    changes_unmapped: int = 0
    states_applied: int = 0
    dry_run: bool = False
    job_id: str | None = None
    status: str | None = None


class GitlleryCommandOperation(BaseModel):
    work_id: UUID
    action: Literal["trash", "restore", "favorite", "tag-add", "tag-remove"]
    value: bool | None = None
    tag: str | None = Field(default=None, min_length=1, max_length=255)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action == "favorite" and self.value is None:
            raise ValueError("favorite requires value")
        if self.action in {"tag-add", "tag-remove"} and not (self.tag or "").strip():
            raise ValueError(f"{self.action} requires tag")
        if self.action not in {"tag-add", "tag-remove"} and self.tag is not None:
            raise ValueError("tag is only valid for tag actions")
        return self


class GitlleryCommandRequest(BaseModel):
    version: Literal[1] = 1
    message: str = Field(min_length=1, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    expected_parent_commit_id: UUID | None = None
    idempotency_key: UUID
    operations: list[GitlleryCommandOperation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_work_limit_and_conflicts(self):
        work_ids = {operation.work_id for operation in self.operations}
        if len(work_ids) > 25:
            raise ValueError("a command may affect at most 25 unique works")
        seen: dict[tuple[UUID, str], object] = {}
        for operation in self.operations:
            family = (
                "visibility"
                if operation.action in {"trash", "restore"}
                else "favorite"
                if operation.action == "favorite"
                else f"tag:{(operation.tag or '').strip().casefold()}"
            )
            key = (operation.work_id, family)
            if key in seen:
                raise ValueError(f"contradictory operations for work {operation.work_id}")
            seen[key] = True
        return self


class GitlleryCommandResponse(BaseModel):
    status: Literal["committed", "noop"]
    commit_id: UUID | None = None
    parent_commit_id: UUID | None = None
    changed: int
    skipped: int
    outbox_state: str
    projected_lag_seconds: float | None = None


class GitlleryVerifyRequest(BaseModel):
    repository_id: str
    deep: bool = False
