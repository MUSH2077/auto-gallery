from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Work(TimestampMixin, Base):
    __tablename__ = "works"

    title: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_nsfw: Mapped[bool] = mapped_column(Boolean, default=False)
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    thumbnail_asset_id: Mapped[UUID | None] = mapped_column(Uuid())
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)

    work_sources = relationship("WorkSource", back_populates="work", cascade="all, delete-orphan")
    work_tags = relationship("WorkTag", back_populates="work", cascade="all, delete-orphan")
