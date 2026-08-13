from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("creator_id", name="uq_subscriptions_creator"),
        CheckConstraint(
            "schedule_mode IS NULL OR schedule_mode IN ('interval', 'fixed_time', 'manual')",
            name="ck_subscriptions_schedule_mode",
        ),
        CheckConstraint(
            "(schedule_mode = 'manual' AND sync_enabled IS FALSE) OR "
            "(schedule_mode IS DISTINCT FROM 'manual' AND sync_enabled IS TRUE)",
            name="ck_subscriptions_schedule_sync_consistent",
        ),
    )

    creator_id: Mapped[UUID] = mapped_column(ForeignKey("creators.id"), nullable=False)
    name: Mapped[str | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_interval_hours: Mapped[int] = mapped_column(Integer, default=6)
    schedule_mode: Mapped[str | None] = mapped_column(String(20), nullable=True,
        comment="NULL=inherit system default, 'interval', 'fixed_time', 'manual'")
    scheduled_times: Mapped[str | None] = mapped_column(String(100), nullable=True,
        comment="NULL=inherit system default, e.g. '03:00,21:00'")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    creator = relationship("Creator", back_populates="subscriptions")
    subscription_sources = relationship("SubscriptionSource", back_populates="subscription")
    download_jobs = relationship("DownloadJob", back_populates="subscription")
