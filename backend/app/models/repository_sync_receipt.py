"""Compact, durable outcomes for repository synchronization attempts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RepositorySyncReceipt(TimestampMixin, Base):
    """One terminal repository synchronization outcome.

    TaskRun/DownloadJob/ImportJob rows are operational state and can be compacted.
    This row is the small domain-owned history that remains visible on the
    repository page after that compaction.
    """

    __tablename__ = "repository_sync_receipts"
    __table_args__ = (
        UniqueConstraint(
            "source_download_job_id",
            name="uq_repository_sync_receipts_download_job",
        ),
        Index(
            "ix_repository_sync_receipts_repository_finished",
            "repository_id",
            "finished_at",
            "id",
        ),
        Index(
            "ix_repository_sync_receipts_status_finished",
            "status",
            "finished_at",
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscription_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_download_job_id: Mapped[UUID] = mapped_column(nullable=False)
    source_import_job_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_task_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome_code: Mapped[str | None] = mapped_column(String(30))
    metadata_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    works_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_excerpt: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    recovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict | None] = mapped_column(JSONB)


class SearchIndexState(TimestampMixin, Base):
    """Durable consistency gate between PostgreSQL and one search index."""

    __tablename__ = "search_index_states"
    __table_args__ = (
        UniqueConstraint("index_uid", name="uq_search_index_states_uid"),
    )

    index_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    database_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    indexed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    database_document_count: Mapped[int | None] = mapped_column(BigInteger)
    index_document_count: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="catching_up")
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class MaintenanceAuditEvent(TimestampMixin, Base):
    """Durable audit for idempotent reconciliation and compaction batches."""

    __tablename__ = "maintenance_audit_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_maintenance_audit_events_key"),
        Index("ix_maintenance_audit_events_type_created", "event_type", "created_at"),
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
