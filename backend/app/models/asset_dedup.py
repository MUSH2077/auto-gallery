from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AssetDedupEvidence(TimestampMixin, Base):
    """Immutable, versioned evidence for one ordered asset pair."""

    __tablename__ = "asset_dedup_evidence"
    __table_args__ = (
        CheckConstraint("left_asset_id <> right_asset_id", name="ck_asset_dedup_evidence_distinct"),
        CheckConstraint(
            "left_asset_id < right_asset_id",
            name="ck_asset_dedup_evidence_ordered",
        ),
        UniqueConstraint(
            "left_asset_id",
            "right_asset_id",
            "algorithm_version",
            "input_digest",
            name="uq_asset_dedup_evidence_pair_revision",
        ),
    )

    left_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    right_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    algorithm_version: Mapped[str] = mapped_column(String(50), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256_equal: Mapped[bool] = mapped_column(nullable=False, default=False)
    phash_distance: Mapped[int | None] = mapped_column(Integer)
    ssim_score: Mapped[float | None] = mapped_column(Float)
    aspect_ratio_delta: Mapped[float | None] = mapped_column(Float)
    visual_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    metadata_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    total_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    hard_gate_passed: Mapped[bool] = mapped_column(nullable=False, default=False)
    facts: Mapped[dict | None] = mapped_column(JSONB)


class VisualAssetGroup(TimestampMixin, Base):
    """A visual identity whose members may have different source bytes."""

    __tablename__ = "visual_asset_groups"

    representative_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)

    members = relationship(
        "VisualAssetMember",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class VisualAssetMember(TimestampMixin, Base):
    __tablename__ = "visual_asset_members"
    __table_args__ = (
        UniqueConstraint("asset_id", name="uq_visual_asset_members_asset"),
        UniqueConstraint("group_id", "asset_id", name="uq_visual_asset_members_group_asset"),
    )

    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("visual_asset_groups.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("asset_dedup_evidence.id", ondelete="SET NULL")
    )
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    quality_facts: Mapped[dict | None] = mapped_column(JSONB)

    group = relationship("VisualAssetGroup", back_populates="members")


class AssetDedupCase(TimestampMixin, Base):
    __tablename__ = "asset_dedup_cases"
    __table_args__ = (
        CheckConstraint("left_asset_id <> right_asset_id", name="ck_asset_dedup_cases_distinct"),
        CheckConstraint(
            "left_asset_id < right_asset_id",
            name="ck_asset_dedup_cases_ordered",
        ),
        CheckConstraint(
            "status IN ('pending', 'merged', 'separate', 'deferred')",
            name="ck_asset_dedup_cases_status",
        ),
        UniqueConstraint("left_asset_id", "right_asset_id", name="uq_asset_dedup_cases_pair"),
    )

    left_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    right_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_dedup_evidence.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    suggested_representative_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)


class AssetDedupDecision(TimestampMixin, Base):
    __tablename__ = "asset_dedup_decisions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('merge', 'separate', 'defer')",
            name="ck_asset_dedup_decisions_action",
        ),
    )

    case_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_dedup_cases.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    representative_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    curation_commit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("curation_commits.id", ondelete="SET NULL")
    )


class AssetDedupOutbox(TimestampMixin, Base):
    __tablename__ = "asset_dedup_outbox"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('observe', 'hardlink', 'quarantine', 'purge')",
            name="ck_asset_dedup_outbox_event_type",
        ),
        CheckConstraint(
            "state IN ('pending', 'processing', 'complete', 'failed')",
            name="ck_asset_dedup_outbox_state",
        ),
        Index(
            "ix_asset_dedup_outbox_processing_lease",
            "state",
            "updated_at",
            "id",
            postgresql_where=text("state = 'processing'"),
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class AssetDedupScan(TimestampMixin, Base):
    """Durable progress for a bounded, resumable asset scan."""

    __tablename__ = "asset_dedup_scans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'complete', 'failed')",
            name="ck_asset_dedup_scans_status",
        ),
    )

    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    cursor_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL")
    )
    assets_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_evaluated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cases_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assets_grouped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_reclaimable: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    options: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
