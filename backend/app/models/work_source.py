from datetime import datetime
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID
from sqlalchemy import DateTime

from app.models.base import Base, TimestampMixin


class WorkSource(TimestampMixin, Base):
    __tablename__ = "work_sources"
    __table_args__ = (
        UniqueConstraint("source", "source_work_id", name="uq_work_sources_source_id"),
    )

    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_work_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    source_creator_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)

    work = relationship("Work", back_populates="work_sources")
    work_source_tags = relationship("WorkSourceTag", back_populates="work_source")
    asset_sources = relationship("AssetSource", back_populates="work_source")
