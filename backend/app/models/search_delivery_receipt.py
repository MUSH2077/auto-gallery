"""Frozen remote writes; completion is independent of a worker's lifetime."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class SearchDeliveryReceipt(TimestampMixin, Base):
    __tablename__ = "search_delivery_receipts"
    __table_args__ = (
        Index("uq_search_delivery_active", text("(true)"), unique=True,
              postgresql_where=text("state NOT IN ('complete', 'failed')")),
    )
    index_uid: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(24))
    versions: Mapped[list] = mapped_column(JSONB, default=list)
    payload: Mapped[list | dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), default="prepared")
    phase: Mapped[str] = mapped_column(String(24), default="settings")
    task_uid: Mapped[int | None] = mapped_column(BigInteger)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    poll_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
