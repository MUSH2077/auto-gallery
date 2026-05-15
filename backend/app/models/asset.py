from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Asset(TimestampMixin, Base):
    __tablename__ = "assets"

    file_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None]
    height: Mapped[int | None]
    duration: Mapped[float | None]
    sha256: Mapped[str | None] = mapped_column(String(64))
    phash: Mapped[str | None] = mapped_column(String(64))
    thumb_sm_path: Mapped[str | None] = mapped_column(String(2000))
    thumb_md_path: Mapped[str | None] = mapped_column(String(2000))
    thumb_lg_path: Mapped[str | None] = mapped_column(String(2000))

    asset_sources = relationship("AssetSource", back_populates="asset")
