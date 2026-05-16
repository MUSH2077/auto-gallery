from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Creator(TimestampMixin, Base):
    __tablename__ = "creators"

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2000))
    is_active: Mapped[bool] = mapped_column(default=True)
    danbooru_artist_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source_creators = relationship("SourceCreator", back_populates="creator")
    creator_links = relationship("CreatorLink", back_populates="creator")
    subscriptions = relationship("Subscription", back_populates="creator")
