from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from uuid import UUID

from app.models.base import Base, TimestampMixin


class SubscriptionSource(TimestampMixin, Base):
    __tablename__ = "subscription_sources"
    __table_args__ = (
        UniqueConstraint("subscription_id", "source_url", name="uq_subscription_sources_sub_url"),
    )

    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscriptions.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_creator_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_successful_auth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    subscription = relationship("Subscription", back_populates="subscription_sources")
