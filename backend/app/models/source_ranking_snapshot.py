from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SourceRankingSnapshot(TimestampMixin, Base):
    __tablename__ = "source_ranking_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "mode",
            "ranking_date",
            "source_work_id",
            name="uq_source_ranking_snapshot_entry",
        ),
        Index(
            "ix_source_ranking_snapshots_current",
            "source",
            "fetched_at",
            "source_work_id",
        ),
    )

    source: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(50), nullable=False)
    ranking_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_work_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_total: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
