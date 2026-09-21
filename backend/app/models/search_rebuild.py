"""Small durable cursors for staging index rebuilds and replay acknowledgment."""
from sqlalchemy import BigInteger, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid import UUID
from app.models.base import Base, TimestampMixin


class SearchRebuild(TimestampMixin, Base):
    __tablename__ = "search_rebuilds"
    __table_args__ = (Index("uq_search_rebuild_active", text("(true)"), unique=True,
                           postgresql_where=text("state NOT IN ('complete', 'failed')")),)
    state: Mapped[str] = mapped_column(String(24), default="running")
    phase: Mapped[str] = mapped_column(String(24), default="settings")
    owner: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[dict] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)


class SearchRebuildReplay(Base):
    __tablename__ = "search_rebuild_replay"
    build_id: Mapped[UUID] = mapped_column(primary_key=True)
    outbox_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
