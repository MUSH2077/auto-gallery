"""Unified task-run models.

``TaskRun`` is the user-facing envelope for any long-running job — downloads,
imports, and admin operations (backup, reindex, disk import) — so they share a
single queue/progress/history surface (the ``/admin/jobs`` page reads this table,
not the per-domain ``download_jobs`` / ``import_jobs`` tables, which stay
authoritative for their own payloads). ``TaskEvent`` is the append-only audit
trail of status transitions for a run.

See ``app.services.tasks.TaskService`` for the write path and
``app.models.task_state`` for the canonical status values.
"""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TaskRun(TimestampMixin, Base):
    """One trackable unit of work, regardless of which pipeline produced it."""

    __tablename__ = "task_runs"

    # Classification: what kind of work and what it acts on.
    kind: Mapped[str] = mapped_column(String(50), nullable=False)        # download | import | admin
    operation_type: Mapped[str | None] = mapped_column(String(80))      # e.g. admin-disk-import, admin-backup
    subject_type: Mapped[str | None] = mapped_column(String(50))        # domain table the run mirrors (download_job, ...)
    subject_id: Mapped[UUID | None] = mapped_column(nullable=True)      # PK in that domain table (no FK: loose coupling)
    parent_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="SET NULL"))  # batch/child grouping

    # Lifecycle — values come from app.models.task_state (enqueued/running/complete/failed/...).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="enqueued")
    # Orthogonal to the business lifecycle. A task can remain ``running`` while
    # yielding its resource permit, or remain ``enqueued`` while waiting for a
    # safe budget. This preserves every existing status transition/API filter.
    resource_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="waiting"
    )
    resource_reason: Mapped[str | None] = mapped_column(String(500))
    attention_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none"
    )
    reason_code: Mapped[str | None] = mapped_column(String(80))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compactable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_name: Mapped[str | None] = mapped_column(String(80))
    rq_job_id: Mapped[str | None] = mapped_column(String(255))          # backing RQ job, for control signals

    # Display + provenance shown in the task list.
    title: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str | None] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(String(2000))

    # Progress snapshot (current/total drive the progress bar; *_data holds richer state).
    progress_stage: Mapped[str | None] = mapped_column(String(50))
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_data: Mapped[dict | None] = mapped_column(JSONB)
    result_data: Mapped[dict | None] = mapped_column(JSONB)
    error_log: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)

    # Scheduling + stale detection.
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)  # see task_state.TaskPriority
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # worker liveness

    parent = relationship("TaskRun", remote_side="TaskRun.id")
    events = relationship("TaskEvent", back_populates="task_run", cascade="all, delete-orphan")


class TaskEvent(Base):
    """Append-only history row for a TaskRun (one per status transition / notable event).

    Cascade-deleted with its parent run via the FK ``ON DELETE CASCADE``, so
    clearing ``task_runs`` (e.g. Data Management -> clear jobs) also clears events.
    """

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_run_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # transition | heartbeat | note | ...
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task_run = relationship("TaskRun", back_populates="events")
