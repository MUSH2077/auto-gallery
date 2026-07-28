from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class DownloadJob(TimestampMixin, Base):
    __tablename__ = "download_jobs"

    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscriptions.id"), nullable=False)
    subscription_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("subscription_sources.id"))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="enqueued")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)
    gallerydl_config_path: Mapped[str | None] = mapped_column(String(2000))
    download_dir: Mapped[str | None] = mapped_column(String(2000))
    manifest: Mapped[dict | None] = mapped_column(JSONB)

    # ── Task Engine fields (Phase 1) ──
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    user_note: Mapped[str | None] = mapped_column(Text)
    operator_name: Mapped[str | None] = mapped_column(String(100))
    operator_action: Mapped[str | None] = mapped_column(String(50))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_pid: Mapped[int | None] = mapped_column(Integer)
    pipeline_stage: Mapped[str | None] = mapped_column(String(50))
    progress_data: Mapped[dict | None] = mapped_column(JSONB)

    subscription = relationship("Subscription", back_populates="download_jobs")
    import_jobs = relationship("ImportJob", back_populates="download_job")
