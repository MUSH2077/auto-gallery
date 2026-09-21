from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CreatorAlias(TimestampMixin, Base):
    __tablename__ = "creator_aliases"
    __table_args__ = (
        UniqueConstraint(
            "creator_id",
            "source",
            "kind",
            "normalized_value",
            name="uq_creator_aliases_identity",
        ),
        Index("ix_creator_aliases_normalized_value", "normalized_value"),
        Index(
            "ix_creator_aliases_creator_current",
            "creator_id",
            "is_current",
        ),
    )

    creator_id: Mapped[UUID] = mapped_column(
        ForeignKey("creators.id", ondelete="CASCADE"),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(String(2000), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(2000), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    source_ref: Mapped[str | None] = mapped_column(String(2000))

    creator = relationship("Creator", back_populates="aliases")
