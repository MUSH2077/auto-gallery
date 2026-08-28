"""Targeted persistence updates after subscription schedule changes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.subscription_sync import (
    next_future_subscription_check_at,
    next_subscription_check_at,
)
from app.models.remote_discovery import UserSubscription, UserSubscriptionSource
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.schemas.schedule import normalize_clock_times
from app.services.subscription_membership import recompute_subscription_membership_cache


def _mapping(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return dict(value) if isinstance(value, dict) else {}


def _canonical_schedule(config: dict | None) -> tuple:
    payload = dict(config or {})
    mode = payload.get("schedule_mode") or "interval"
    rule = _mapping(payload.get("schedule_rule")) or None
    if mode == "fixed_time":
        try:
            times = normalize_clock_times(payload.get("scheduled_times") or [])
        except ValueError:
            mode, rule = "interval", None
        else:
            mode = "calendar"
            rule = {"frequency": "daily", "times": times}
    elif mode == "calendar" and rule is None:
        try:
            times = normalize_clock_times(payload.get("scheduled_times") or [])
        except ValueError:
            times = []
        rule = {"frequency": "daily", "times": times} if times else None
    if mode != "calendar":
        rule = None
    return (
        mode,
        rule,
        str(payload.get("timezone") or "UTC"),
        int(payload.get("default_sync_interval_hours") or 6),
    )


def subscription_schedule_changed(old: dict | None, new: dict | None) -> bool:
    """Ignore operational toggles that do not change any logical due time."""

    return _canonical_schedule(old) != _canonical_schedule(new)


def _next_replanned_at(
    subscription: Subscription,
    source: SubscriptionSource,
    config: dict,
    now: datetime,
) -> datetime | None:
    mode = subscription.schedule_mode or config.get("schedule_mode", "interval")
    if (
        not subscription.is_active
        or not subscription.sync_enabled
        or mode == "manual"
        or not source.is_enabled
    ):
        return None
    if mode in {"calendar", "fixed_time"}:
        return next_future_subscription_check_at(subscription, config, now)

    interval_hours = subscription.sync_interval_hours or int(
        config.get("default_sync_interval_hours", 6)
    )
    interval = timedelta(hours=max(1, int(interval_hours)))
    last_synced = source.last_synced_at
    if last_synced is not None:
        if last_synced.tzinfo is None:
            last_synced = last_synced.replace(tzinfo=timezone.utc)
        candidate = last_synced.astimezone(timezone.utc) + interval
        if candidate > now:
            return candidate
    return now + interval


async def replan_subscription_sources(
    db: AsyncSession,
    subscription: Subscription,
    config: dict,
    *,
    now: datetime | None = None,
    sources: list[SubscriptionSource] | None = None,
) -> int:
    """Move one subscription to its next future slot without enqueueing work."""

    now = now or datetime.now(timezone.utc)
    if sources is None:
        sources = list((await db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id == subscription.id)
            .order_by(SubscriptionSource.id)
            .with_for_update()
        )).scalars())
    for source in sources:
        source.next_sync_at = _next_replanned_at(subscription, source, config, now)
    await db.flush()
    return len(sources)


def next_user_subscription_check_at(
    membership: UserSubscription,
    config: dict,
    last_synced_at: datetime | None,
    last_attempted_at: datetime | None,
    now: datetime,
) -> datetime | None:
    """Calculate one private due time, including whole-policy inheritance."""

    subject: UserSubscription | SimpleNamespace = membership
    if membership.schedule_mode is None:
        # NULL is the explicit private "inherit" marker.  Do not let copied
        # legacy interval/calendar fields shadow a later system-policy change.
        subject = SimpleNamespace(
            schedule_mode=None,
            schedule_rule=None,
            scheduled_times=None,
            sync_interval_hours=None,
            created_at=membership.created_at,
        )
    return next_subscription_check_at(
        subject,
        config,
        last_synced_at,
        last_attempted_at,
        now,
    )


async def replan_user_subscription_sources(
    db: AsyncSession,
    membership: UserSubscription,
    config: dict,
    *,
    now: datetime | None = None,
    bindings: list[UserSubscriptionSource] | None = None,
) -> int:
    """Replan every private binding, then refresh the sole canonical cache."""

    now = now or datetime.now(timezone.utc)
    if bindings is None:
        bindings = list(
            (
                await db.execute(
                    select(UserSubscriptionSource)
                    .where(
                        UserSubscriptionSource.user_subscription_id == membership.id
                    )
                    .order_by(UserSubscriptionSource.id)
                    .with_for_update(of=UserSubscriptionSource)
                )
            ).scalars()
        )
    enabled = membership.is_active and membership.sync_enabled
    for binding in bindings:
        binding.next_sync_at = (
            next_user_subscription_check_at(
                membership,
                config,
                binding.last_synced_at,
                binding.last_attempted_at,
                now,
            )
            if enabled and binding.is_enabled
            else None
        )
    await recompute_subscription_membership_cache(db, membership.subscription_id)
    await db.flush()
    return len(bindings)


async def replan_inherited_subscription_sources(
    db: AsyncSession,
    config: dict,
    *,
    now: datetime | None = None,
) -> int:
    """Replan only subscriptions that inherit the changed system schedule."""

    now = now or datetime.now(timezone.utc)
    private_rows = list(
        (
            await db.execute(
                select(UserSubscription, UserSubscriptionSource)
                .join(
                    UserSubscriptionSource,
                    UserSubscriptionSource.user_subscription_id
                    == UserSubscription.id,
                )
                .where(UserSubscription.schedule_mode.is_(None))
                .order_by(
                    UserSubscription.subscription_id,
                    UserSubscription.id,
                    UserSubscriptionSource.id,
                )
                .with_for_update(of=UserSubscriptionSource)
            )
        ).all()
    )
    affected_subscription_ids: set[UUID] = set()
    for membership, binding in private_rows:
        binding.next_sync_at = (
            next_user_subscription_check_at(
                membership,
                config,
                binding.last_synced_at,
                binding.last_attempted_at,
                now,
            )
            if membership.is_active
            and membership.sync_enabled
            and binding.is_enabled
            else None
        )
        affected_subscription_ids.add(membership.subscription_id)

    # Canonical-only rows remain supported for old deployments and focused
    # legacy service tests.  Once a private member exists, canonical due state
    # is exclusively the aggregate below.
    legacy_rows = list(
        (
            await db.execute(
                select(Subscription, SubscriptionSource)
                .join(
                    SubscriptionSource,
                    SubscriptionSource.subscription_id == Subscription.id,
                )
                .where(
                    Subscription.schedule_mode.is_(None),
                    Subscription.id.not_in(
                        select(UserSubscription.subscription_id)
                    ),
                )
                .order_by(Subscription.id, SubscriptionSource.id)
                .with_for_update(of=SubscriptionSource)
            )
        ).all()
    )
    for subscription, source in legacy_rows:
        source.next_sync_at = _next_replanned_at(subscription, source, config, now)
    for subscription_id in sorted(affected_subscription_ids, key=str):
        await recompute_subscription_membership_cache(db, subscription_id)
    await db.flush()
    return len(private_rows) + len(legacy_rows)
