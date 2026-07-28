from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class DownloadJob(TimestampMixin, Base):
    __tablename__ = "download_jobs"

    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscriptions.id"), nullable=False)
    subscription_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("subscription_sources.id"))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)
    gallerydl_config_path: Mapped[str | None] = mapped_column(String(2000))
    download_dir: Mapped[str | None] = mapped_column(String(2000))
    manifest: Mapped[dict | None] = mapped_column(JSONB)

    subscription = relationship("Subscription", back_populates="download_jobs")
    import_jobs = relationship("ImportJob", back_populates="download_job")
