from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class AssetSource(TimestampMixin, Base):
    __tablename__ = "asset_sources"
    __table_args__ = (
        UniqueConstraint("source", "source_asset_id", name="uq_asset_sources_source_id"),
    )

    asset_id: Mapped[UUID] = mapped_column(ForeignKey("assets.id"), nullable=False)
    work_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("work_sources.id"))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_asset_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB)

    asset = relationship("Asset", back_populates="asset_sources")
    work_source = relationship("WorkSource", back_populates="asset_sources")
