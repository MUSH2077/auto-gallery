"""Durable, coalescing work queue for derived search projections."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SearchProjectionOutbox(TimestampMixin, Base):
    """Latest required search action for one logical document.

    The unique entity key deliberately coalesces repeated mutations. ``version``
    protects a newly queued action from an older worker acknowledging it, while
    ``lease_until`` makes an interrupted drain automatically resumable.
    Completed rows are retained briefly so a full staging rebuild can replay
    mutations that happened after its keyset walk began.
    """

    __tablename__ = "search_projection_outbox"
    __table_args__ = (
        UniqueConstraint(
            "index_uid",
            "entity_id",
            name="uq_search_projection_outbox_entity",
        ),
        CheckConstraint(
            "action IN ('upsert', 'delete')",
            name="ck_search_projection_outbox_action",
        ),
        Index(
            "ix_search_projection_outbox_ready",
            "available_at",
            "updated_at",
            "id",
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index(
            "ix_search_projection_outbox_pending_index",
            "index_uid",
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index(
            "ix_search_projection_outbox_rebuild_replay",
            "index_uid",
            "updated_at",
            "id",
        ),
    )

    index_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False, default="upsert")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
