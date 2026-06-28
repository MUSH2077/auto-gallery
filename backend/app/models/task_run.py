from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class TaskRun(TimestampMixin, Base):
    __tablename__ = "task_runs"

    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    operation_type: Mapped[str | None] = mapped_column(String(80))
    subject_type: Mapped[str | None] = mapped_column(String(50))
    subject_id: Mapped[UUID | None] = mapped_column(nullable=True)
    parent_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("task_runs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="enqueued")
    queue_name: Mapped[str | None] = mapped_column(String(80))
    rq_job_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str | None] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    progress_stage: Mapped[str | None] = mapped_column(String(50))
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_data: Mapped[dict | None] = mapped_column(JSONB)
    result_data: Mapped[dict | None] = mapped_column(JSONB)
    error_log: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    parent = relationship("TaskRun", remote_side="TaskRun.id")
    events = relationship("TaskEvent", back_populates="task_run", cascade="all, delete-orphan")


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_run_id: Mapped[UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task_run = relationship("TaskRun", back_populates="events")
