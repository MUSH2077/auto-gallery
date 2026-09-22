from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, Computed, DateTime, Float, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Work(TimestampMixin, Base):
    __tablename__ = "works"
    __table_args__ = (
        Index("ix_works_heat_score_id", "heat_score", "id"),
        Index("ix_works_shuffle_key_id", "shuffle_key", "id"),
    )

    title: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_nsfw: Mapped[bool] = mapped_column(Boolean, default=False)
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    thumbnail_asset_id: Mapped[UUID | None] = mapped_column(Uuid())
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    shuffle_key: Mapped[int] = mapped_column(
        BigInteger,
        Computed(
            "((('x' || substr(md5(id::text), 1, 16))::bit(64)::bigint) "
            "& 9223372036854775807)",
            persisted=True,
        ),
        nullable=False,
    )
    heat_score: Mapped[float | None] = mapped_column(Float)
    heat_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    work_sources = relationship("WorkSource", back_populates="work", cascade="all, delete-orphan")
    work_tags = relationship("WorkTag", back_populates="work", cascade="all, delete-orphan")
