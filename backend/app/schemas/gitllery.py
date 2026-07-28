from pydantic import BaseModel, Field


class GitlleryRepoStatus(BaseModel):
    repository_id: str
    source: str
    creator_dir: str
    exists: bool
    behind: int
    object_integrity_ok: bool
    drift: list[str] = Field(default_factory=list)
    clean: bool


class GitlleryStatusResponse(BaseModel):
    repositories: list[GitlleryRepoStatus]
    missing_repos: int
    behind_total: int
    deep: bool = False
    # True when the projection checkpoint is absent (fresh redis, post-rebuild):
    # pending counts are unknown and the queued reconcile job must run. status
    # deliberately has no inline full-history fallback (OOM vector).
    needs_reconcile: bool = False


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
