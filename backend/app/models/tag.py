from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Tag(TimestampMixin, Base):
    __tablename__ = "tags"

    normalized_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    category: Mapped[str | None] = mapped_column(String(50))

    work_tags = relationship("WorkTag", back_populates="tag")
    work_source_tags = relationship("WorkSourceTag", back_populates="tag")
