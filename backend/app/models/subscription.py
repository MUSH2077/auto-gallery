from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from uuid import UUID

from app.models.base import Base, TimestampMixin


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("creator_id", name="uq_subscriptions_creator"),
    )

    creator_id: Mapped[UUID] = mapped_column(ForeignKey("creators.id"), nullable=False)
    name: Mapped[str | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sync_interval_hours: Mapped[int] = mapped_column(Integer, default=6)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    creator = relationship("Creator", back_populates="subscriptions")
    subscription_sources = relationship("SubscriptionSource", back_populates="subscription")
    download_jobs = relationship("DownloadJob", back_populates="subscription")
