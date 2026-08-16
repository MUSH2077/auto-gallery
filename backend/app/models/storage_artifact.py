from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class StorageArtifact(TimestampMixin, Base):
    """Durable inventory and import state for files managed by auto-gallery."""

    __tablename__ = "storage_artifacts"
    __table_args__ = (
        UniqueConstraint("storage_root", "file_path", name="uq_storage_artifacts_root_path"),
        Index("ix_storage_artifacts_download_state", "download_job_id", "artifact_type", "state"),
        Index("ix_storage_artifacts_source_work", "source", "source_work_id"),
        Index("ix_storage_artifacts_lease", "state", "lease_expires_at"),
        Index(
            "ix_storage_artifacts_lease_token",
            "lease_token",
            postgresql_where=text("lease_token IS NOT NULL"),
        ),
    )

    storage_root: Mapped[str] = mapped_column(String(20), nullable=False)
    file_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    creator_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    source_work_id: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(30), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    content_version: Mapped[str | None] = mapped_column(String(64))
    download_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("download_jobs.id", ondelete="SET NULL"))
    import_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_jobs.id", ondelete="SET NULL"))
    # Ownership belongs to a concrete ImportJob *execution*, not merely the
    # durable job row.  Retries receive a new token and wait for (or recover)
    # leases held by an older process instead of processing the same files.
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
