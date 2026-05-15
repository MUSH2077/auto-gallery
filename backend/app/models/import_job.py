from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class ImportJob(TimestampMixin, Base):
    __tablename__ = "import_jobs"

    download_job_id: Mapped[UUID] = mapped_column(ForeignKey("download_jobs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_log: Mapped[str | None] = mapped_column(Text)

    download_job = relationship("DownloadJob", back_populates="import_jobs")
