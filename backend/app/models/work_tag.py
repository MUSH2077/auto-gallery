from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class WorkTag(TimestampMixin, Base):
    __tablename__ = "work_tags"
    __table_args__ = (
        UniqueConstraint("work_id", "tag_id", name="uq_work_tags_work_tag"),
    )

    work_id: Mapped[UUID] = mapped_column(ForeignKey("works.id"), nullable=False)
    tag_id: Mapped[UUID] = mapped_column(ForeignKey("tags.id"), nullable=False)
    source: Mapped[str | None] = mapped_column()

    work = relationship("Work", back_populates="work_tags")
    tag = relationship("Tag", back_populates="work_tags")
