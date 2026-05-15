from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class WorkSourceTag(TimestampMixin, Base):
    __tablename__ = "work_source_tags"
    __table_args__ = (
        UniqueConstraint("work_source_id", "tag_id", name="uq_work_source_tags_source_tag"),
    )

    work_source_id: Mapped[UUID] = mapped_column(ForeignKey("work_sources.id"), nullable=False)
    tag_id: Mapped[UUID] = mapped_column(ForeignKey("tags.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    original_name: Mapped[str | None] = mapped_column(String(500))

    work_source = relationship("WorkSource", back_populates="work_source_tags")
    tag = relationship("Tag", back_populates="work_source_tags")
