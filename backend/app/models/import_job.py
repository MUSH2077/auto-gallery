from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ImportJob(TimestampMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        Index(
            "uq_import_jobs_execution_token",
            "execution_token",
            unique=True,
        ),
    )

    download_job_id: Mapped[UUID] = mapped_column(ForeignKey("download_jobs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="enqueued")
    error_log: Mapped[str | None] = mapped_column(Text)

    # ── Task Engine fields (Phase 1) ──
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    user_note: Mapped[str | None] = mapped_column(Text)
    operator_name: Mapped[str | None] = mapped_column(String(100))
    operator_action: Mapped[str | None] = mapped_column(String(50))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    import_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_import_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # A fresh token identifies one concrete worker execution.  The job UUID is
    # intentionally not sufficient here: retries reuse it and must never be
    # able to take over an unexpired artifact lease from an earlier process.
    execution_token: Mapped[UUID | None] = mapped_column(nullable=True)
    execution_attempt: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    progress_stage: Mapped[str | None] = mapped_column(String(50))
    progress_works_done: Mapped[int | None] = mapped_column(Integer)
    progress_works_total: Mapped[int | None] = mapped_column(Integer)
    progress_data: Mapped[dict | None] = mapped_column(JSONB)

    download_job = relationship("DownloadJob", back_populates="import_jobs")
