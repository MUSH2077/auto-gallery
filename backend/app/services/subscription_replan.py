"""Targeted persistence updates after subscription schedule changes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.subscription_sync import next_future_subscription_check_at
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.schemas.schedule import normalize_clock_times


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


async def replan_inherited_subscription_sources(
    db: AsyncSession,
    config: dict,
    *,
    now: datetime | None = None,
) -> int:
    """Replan only subscriptions that inherit the changed system schedule."""

    now = now or datetime.now(timezone.utc)
    rows = list((await db.execute(
        select(Subscription, SubscriptionSource)
        .join(
            SubscriptionSource,
            SubscriptionSource.subscription_id == Subscription.id,
        )
        .where(Subscription.schedule_mode.is_(None))
        .order_by(Subscription.id, SubscriptionSource.id)
        .with_for_update(of=SubscriptionSource)
    )).all())
    for subscription, source in rows:
        source.next_sync_at = _next_replanned_at(subscription, source, config, now)
    await db.flush()
    return len(rows)
