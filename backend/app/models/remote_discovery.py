"""Private memberships and remote-follow discovery persistence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class UserSubscription(TimestampMixin, Base):
    """One user's private membership in a globally shared subscription."""

    __tablename__ = "user_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "subscription_id", name="uq_user_subscriptions_user_subscription"),
        UniqueConstraint("id", "user_id", "subscription_id", name="uq_user_subscriptions_id_owner_subscription"),
        CheckConstraint(
            "schedule_mode IS NULL OR schedule_mode IN ('interval', 'calendar', 'manual')",
            name="ck_user_subscriptions_schedule_mode",
        ),
        CheckConstraint(
            "(schedule_mode = 'manual' AND sync_enabled IS FALSE) OR "
            "(schedule_mode IS DISTINCT FROM 'manual' AND sync_enabled IS TRUE)",
            name="ck_user_subscriptions_schedule_sync_consistent",
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    sync_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=6, server_default=text("6"))
    schedule_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    schedule_rule: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    scheduled_times: Mapped[str | None] = mapped_column(String(100), nullable=True)

    user = relationship("User", back_populates="user_subscriptions")
    subscription = relationship("Subscription", back_populates="user_subscriptions")
    subscription_sources = relationship(
        "UserSubscriptionSource",
        back_populates="user_subscription",
        foreign_keys="UserSubscriptionSource.user_subscription_id",
    )


class RemoteAccount(TimestampMixin, Base):
    """A user's remote account; encrypted credential material is never serialized."""

    __tablename__ = "remote_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "source", name="uq_remote_accounts_user_source"),
        UniqueConstraint("id", "user_id", name="uq_remote_accounts_id_owner"),
        CheckConstraint("source IN ('pixiv', 'x', 'bilibili')", name="ck_remote_accounts_source"),
        CheckConstraint(
            "auth_method IS NULL OR "
            "(source = 'pixiv' AND auth_method = 'refresh_token') OR "
            "(source = 'x' AND auth_method IN ('oauth2', 'cookie')) OR "
            "(source = 'bilibili' AND auth_method = 'sessdata')",
            name="ck_remote_accounts_auth_method",
        ),
        CheckConstraint(
            "auto_import_min_confidence IN ('high', 'medium', 'low')",
            name="ck_remote_accounts_auto_import_confidence",
        ),
        CheckConstraint(
            "auto_import_limit BETWEEN 1 AND 200",
            name="ck_remote_accounts_auto_import_limit",
        ),
        CheckConstraint(
            "credential_generation >= 0",
            name="ck_remote_accounts_credential_generation",
        ),
        Index(
            "ix_remote_accounts_next_scan_due",
            "next_scan_at",
            "id",
            postgresql_where=text("is_enabled IS TRUE"),
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    remote_user_id: Mapped[str | None] = mapped_column(String(255))
    remote_username: Mapped[str | None] = mapped_column(String(255))
    auth_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    collection_selectors: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credential_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    credential_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    auth_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    auth_error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_cursor: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_scan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_scan_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24, server_default=text("24"))
    auto_import_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    auto_import_min_confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="high", server_default=text("'high'"))
    auto_import_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=25, server_default=text("25"))

    user = relationship("User", back_populates="remote_accounts")
    membership_sources = relationship(
        "UserSubscriptionSource",
        back_populates="remote_account",
        foreign_keys="UserSubscriptionSource.remote_account_id",
    )
    discovery_candidates = relationship(
        "DiscoveryCandidate",
        back_populates="remote_account",
        foreign_keys="DiscoveryCandidate.remote_account_id",
    )

    def __repr__(self) -> str:
        return f"RemoteAccount(id={self.id!r}, user_id={self.user_id!r}, source={self.source!r})"


class UserSubscriptionSource(TimestampMixin, Base):
    """A member's private scheduling and authentication policy for one source."""

    __tablename__ = "user_subscription_sources"
    __table_args__ = (
        UniqueConstraint(
            "user_subscription_id",
            "subscription_source_id",
            name="uq_user_subscription_sources_membership_source",
        ),
        ForeignKeyConstraint(
            ["user_subscription_id", "user_id", "subscription_id"],
            ["user_subscriptions.id", "user_subscriptions.user_id", "user_subscriptions.subscription_id"],
            name="fk_user_subscription_sources_membership_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["subscription_source_id", "subscription_id"],
            ["subscription_sources.id", "subscription_sources.subscription_id"],
            name="fk_user_subscription_sources_source_subscription",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["remote_account_id", "user_id"],
            ["remote_accounts.id", "remote_accounts.user_id"],
            name="fk_user_subscription_sources_remote_account_owner",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_user_subscription_sources_next_sync_due",
            "next_sync_at",
            "id",
            postgresql_where=text("is_enabled IS TRUE AND auth_healthy IS TRUE"),
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False)
    user_subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    subscription_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscription_sources.id", ondelete="RESTRICT"), nullable=False
    )
    remote_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("remote_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    last_successful_auth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    auth_error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_auth_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user_subscription = relationship(
        "UserSubscription",
        back_populates="subscription_sources",
        foreign_keys=[user_subscription_id],
    )
    subscription_source = relationship(
        "SubscriptionSource",
        back_populates="user_subscription_sources",
        foreign_keys=[subscription_source_id],
    )
    remote_account = relationship(
        "RemoteAccount",
        back_populates="membership_sources",
        foreign_keys=[remote_account_id],
    )


class DiscoveryCandidate(TimestampMixin, Base):
    """One account-private remote following discovered during a scan."""

    __tablename__ = "discovery_candidates"
    __table_args__ = (
        UniqueConstraint("remote_account_id", "source_creator_id", name="uq_discovery_candidates_account_creator"),
        ForeignKeyConstraint(
            ["remote_account_id", "user_id"],
            ["remote_accounts.id", "remote_accounts.user_id"],
            name="fk_discovery_candidates_account_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_subscription_id", "user_id", "subscription_id"],
            ["user_subscriptions.id", "user_subscriptions.user_id", "user_subscriptions.subscription_id"],
            name="fk_discovery_candidates_membership_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "user_subscription_id IS NULL OR subscription_id IS NOT NULL",
            name="ck_discovery_candidates_membership_subscription",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_discovery_candidates_confidence",
        ),
        CheckConstraint(
            "state IN ('pending', 'dismissed', 'imported', 'conflict')",
            name="ck_discovery_candidates_state",
        ),
        Index("ix_discovery_candidates_account_state", "remote_account_id", "state", "id"),
    )

    remote_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("remote_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    source_creator_id: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    candidate_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="low", server_default=text("'low'"))
    confidence_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default=text("'pending'"))
    subscription_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=True
    )
    user_subscription_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("user_subscriptions.id", ondelete="RESTRICT"), nullable=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_following: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    remote_account = relationship(
        "RemoteAccount",
        back_populates="discovery_candidates",
        foreign_keys=[remote_account_id],
    )
    subscription = relationship("Subscription", back_populates="discovery_candidates")
    user_subscription = relationship("UserSubscription", foreign_keys=[user_subscription_id])
