"""Scheduler-owned identities outlive deletable operational and source rows."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SchedulerBatch(TimestampMixin, Base):
    __tablename__ = "scheduler_batches"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_scheduler_batch_request"),
        UniqueConstraint("task_id", name="uq_scheduler_batch_task"),
        Index("uq_scheduler_batch_active", text("(true)"), unique=True, postgresql_where=text("state = 'active'")),
    )
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_task_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB)


class SchedulerBatchItem(TimestampMixin, Base):
    __tablename__ = "scheduler_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_id", name="uq_scheduler_batch_source"),
        Index("ix_scheduler_batch_item_due", "batch_id", "next_retry_at", "id"),
        Index("ix_scheduler_batch_item_download", "download_job_id"),
    )
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("scheduler_batches.id", ondelete="RESTRICT"), nullable=False)
    source_id: Mapped[UUID] = mapped_column(nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    download_job_id: Mapped[UUID | None] = mapped_column(nullable=True)
    child_task_id: Mapped[UUID | None] = mapped_column(nullable=True)
    owns_download: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[dict | None] = mapped_column(JSONB)
