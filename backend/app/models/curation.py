from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CurationCommit(TimestampMixin, Base):
    __tablename__ = "curation_commits"

    parent_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    actor_type: Mapped[str] = mapped_column(String(50), default="admin", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(String(100), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(500), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reverts_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    stats: Mapped[dict | None] = mapped_column(JSONB)
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)


class CurationChange(TimestampMixin, Base):
    __tablename__ = "curation_changes"

    commit_id: Mapped[UUID] = mapped_column(ForeignKey("curation_commits.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    diff: Mapped[dict | None] = mapped_column(JSONB)
    impact: Mapped[dict | None] = mapped_column(JSONB)


class WorkCurationState(TimestampMixin, Base):
    __tablename__ = "work_curation_states"

    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), unique=True, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="visible", nullable=False)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trashed_by_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    restored_by_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_by_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    reason: Mapped[str | None] = mapped_column(Text)


class CreatorCurationState(TimestampMixin, Base):
    __tablename__ = "creator_curation_states"

    creator_id: Mapped[UUID] = mapped_column(ForeignKey("creators.id"), unique=True, nullable=False)
    visibility: Mapped[str] = mapped_column(String(50), default="visible", nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    restored_by_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    reason: Mapped[str | None] = mapped_column(Text)


class AssetStorageState(TimestampMixin, Base):
    __tablename__ = "asset_storage_states"

    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), unique=True, nullable=False)
    storage_state: Mapped[str] = mapped_column(String(50), default="available", nullable=False)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_by_commit_id: Mapped[UUID | None] = mapped_column(ForeignKey("curation_commits.id"))
    bytes_reclaimed: Mapped[int] = mapped_column(default=0, nullable=False)
    missing_files: Mapped[list | None] = mapped_column(JSONB)
    served_by_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    dedup_kind: Mapped[str | None] = mapped_column(String(30))
    quarantine_path: Mapped[str | None] = mapped_column(String(2000))
    quarantine_sha256: Mapped[str | None] = mapped_column(String(64))
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
