from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class SourceCreator(TimestampMixin, Base):
    __tablename__ = "source_creators"
    __table_args__ = (
        UniqueConstraint("source", "source_creator_id", name="uq_source_creators_source_id"),
    )

    creator_id: Mapped[UUID | None] = mapped_column(ForeignKey("creators.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_creator_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000))
    display_name: Mapped[str | None] = mapped_column(String(500))
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)

    creator = relationship("Creator", back_populates="source_creators")
