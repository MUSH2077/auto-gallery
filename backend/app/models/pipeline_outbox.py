"""Durable, idempotent work queues for eventually-consistent pipeline stages.

These rows are committed with the authoritative PostgreSQL mutation.  Redis is
only a wake-up mechanism: if publishing an RQ job fails, a bounded recovery
scan can safely resume from the outbox without replaying the import itself.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


OUTBOX_STATES = ("pending", "processing", "complete", "failed")


class MediaDerivativeOutbox(TimestampMixin, Base):
    """Missing media metadata/derivatives for one asset.

    One row per asset intentionally coalesces repeated requests.  ``requested``
    is a set-like JSON object (for example ``{"sha256": true, "phash": true}``)
    so a lazy HTTP request and the importer can safely merge work.
    """

    __tablename__ = "media_derivative_outbox"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_media_derivative_outbox_state",
        ),
        Index(
            "ix_media_derivative_outbox_ready",
            "available_at",
            "id",
            postgresql_where=text("state IN ('pending', 'failed')"),
        ),
        Index(
            "ix_media_derivative_outbox_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("state = 'processing'"),
        ),
    )

    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    requested: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    algorithm_version: Mapped[str] = mapped_column(
        String(80), nullable=False, default="media-v1"
    )
    source_size: Mapped[int | None] = mapped_column(BigInteger)
    source_mtime_ns: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class GitlleryProjectionOutbox(TimestampMixin, Base):
    """One coalesced disk-projection intent for a curation commit."""

    __tablename__ = "gitllery_projection_outbox"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_gitllery_projection_outbox_state",
        ),
        Index(
            "ix_gitllery_projection_outbox_ready",
            "available_at",
            "id",
            postgresql_where=text("state IN ('pending', 'failed')"),
        ),
        Index(
            "ix_gitllery_projection_outbox_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("state = 'processing'"),
        ),
    )

    commit_id: Mapped[UUID] = mapped_column(
        ForeignKey("curation_commits.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    repository_id: Mapped[UUID | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    projection_stats: Mapped[dict | None] = mapped_column(JSONB)


class GitlleryProjectionTarget(TimestampMixin, Base):
    """Durable per-repository fan-out for one authoritative intent."""

    __tablename__ = "gitllery_projection_targets"
    __table_args__ = (
        UniqueConstraint(
            "commit_id",
            "repository_key",
            name="uq_gitllery_projection_target_commit_repo",
        ),
        CheckConstraint(
            "state IN ('pending', 'processing', 'complete', 'failed', 'blocked')",
            name="ck_gitllery_projection_target_state",
        ),
        Index(
            "ix_gitllery_projection_targets_ready",
            "available_at",
            "id",
            postgresql_where=text("state IN ('pending', 'failed')"),
        ),
        Index(
            "ix_gitllery_projection_targets_repo_order",
            "repository_key",
            "commit_created_at",
            "commit_id",
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("gitllery_projection_outbox.id", ondelete="CASCADE"),
        nullable=False,
    )
    commit_id: Mapped[UUID] = mapped_column(
        ForeignKey("curation_commits.id", ondelete="CASCADE"), nullable=False
    )
    commit_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    repository_key: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    creator_dir: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid())
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    segment_digest: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)


class GitlleryRepositoryState(TimestampMixin, Base):
    """Small PostgreSQL fence/checkpoint for a portable repository."""

    __tablename__ = "gitllery_repository_state"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('capture', 'shadow', 'active', 'blocked')",
            name="ck_gitllery_repository_state_mode",
        ),
    )

    repository_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    creator_dir: Mapped[str] = mapped_column(String(1000), nullable=False)
    product_version: Mapped[str] = mapped_column(String(20), nullable=False, default="v1")
    format_id: Mapped[str] = mapped_column(
        String(80), nullable=False, default="gitllery-segment"
    )
    format_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="capture")
    generation: Mapped[str] = mapped_column(String(100), nullable=False)
    head_segment: Mapped[str | None] = mapped_column(String(64))
    last_complete_commit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("curation_commits.id", ondelete="SET NULL")
    )
    last_complete_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    segment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    commit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class GitlleryBuild(TimestampMixin, Base):
    """Checkpoint for a manual side-by-side build or restore workflow."""

    __tablename__ = "gitllery_builds"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('build', 'verify', 'restore')",
            name="ck_gitllery_build_kind",
        ),
        CheckConstraint(
            "state IN ('pending', 'running', 'staged', 'complete', 'failed', 'cancelled')",
            name="ck_gitllery_build_state",
        ),
    )

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    generation: Mapped[str] = mapped_column(String(100), nullable=False)
    repository_key: Mapped[str | None] = mapped_column(String(500))
    high_water_commit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("curation_commits.id", ondelete="SET NULL")
    )
    cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_commit_id: Mapped[UUID | None] = mapped_column(Uuid())
    summary_hash: Mapped[str | None] = mapped_column(String(64))
    stats: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)


class ImportCurationOutbox(TimestampMixin, Base):
    """Imported work awaiting curation and its small library projection.

    Curation and ``metadata.json`` deliberately have independent state.  The
    authoritative work rows and both intents are committed together, while the
    filesystem write happens later without holding a PostgreSQL transaction.
    """

    __tablename__ = "import_curation_outbox"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_import_curation_outbox_state",
        ),
        Index(
            "ix_import_curation_outbox_ready",
            "available_at",
            "id",
            postgresql_where=text("state IN ('pending', 'failed')"),
        ),
        Index(
            "ix_import_curation_outbox_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("state = 'processing'"),
        ),
        CheckConstraint(
            "metadata_state IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_import_curation_outbox_metadata_state",
        ),
        Index(
            "ix_import_curation_outbox_metadata_ready",
            "metadata_available_at",
            "id",
            postgresql_where=text("metadata_state IN ('pending', 'failed')"),
        ),
        Index(
            "ix_import_curation_outbox_metadata_lease",
            "metadata_lease_expires_at",
            "id",
            postgresql_where=text("metadata_state = 'processing'"),
        ),
    )

    work_id: Mapped[UUID] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    creator_id: Mapped[UUID | None] = mapped_column(nullable=True)
    repository_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(String(50))
    source_work_id: Mapped[str | None] = mapped_column(String(255))
    batch_key: Mapped[str | None] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    curation_commit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("curation_commits.id", ondelete="SET NULL")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    # Relative to ``settings.library_root``.  Legacy rows may be NULL; the
    # worker can reconstruct the same path from their committed WorkSource.
    metadata_path: Mapped[str | None] = mapped_column(String(2000))
    metadata_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    metadata_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    metadata_lease_token: Mapped[UUID | None] = mapped_column(Uuid())
    metadata_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    metadata_last_error: Mapped[str | None] = mapped_column(Text)
