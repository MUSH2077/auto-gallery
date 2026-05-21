from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Work(TimestampMixin, Base):
    __tablename__ = "works"

    title: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[str | None] = mapped_column(String(100))
    is_nsfw: Mapped[bool] = mapped_column(Boolean, default=False)
    thumbnail_asset_id: Mapped[str | None] = mapped_column(String(36))
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)

    work_sources = relationship("WorkSource", back_populates="work")
    work_tags = relationship("WorkTag", back_populates="work")
