from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class CreatorLink(TimestampMixin, Base):
    __tablename__ = "creator_links"

    creator_id: Mapped[UUID] = mapped_column(ForeignKey("creators.id"), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    link_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    is_verified: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    creator = relationship("Creator", back_populates="creator_links")
