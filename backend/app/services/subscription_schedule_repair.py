"""Dry-run and idempotent migration of legacy manual subscriptions."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.subscription_sync import next_future_subscription_check_at
from app.models.repository_sync_receipt import MaintenanceAuditEvent
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services.search_projection_outbox import request_search_projection
from app.services.settings import get_scheduler_config
from app.services.subscription_source_selection import (
    eligible_source,
    lock_subscription_sources,
    select_primary_subscription_source,
)


def _automatic_projection(subscription: Subscription) -> SimpleNamespace:
    return SimpleNamespace(
        schedule_mode=None,
        scheduled_times=subscription.scheduled_times,
        sync_interval_hours=subscription.sync_interval_hours,
    )


async def build_subscription_schedule_migration_plan(
    db: AsyncSession,
    *,
    subscription_ids: list[UUID] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build a read-only snapshot whose selected source is frozen by UUID."""

    now = now or datetime.now(timezone.utc)
    statement = select(Subscription).order_by(Subscription.id)
    if subscription_ids is None:
        statement = statement.where(
            Subscription.schedule_mode == "manual",
            or_(
                Subscription.sync_enabled.is_(False),
                Subscription.sync_enabled.is_(True),
            ),
        )
    else:
        statement = statement.where(Subscription.id.in_(subscription_ids))
    subscriptions = list((await db.execute(statement)).scalars())
    config = await get_scheduler_config(db)
    result: list[dict[str, Any]] = []
    for subscription in subscriptions:
        sources = list((await db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id == subscription.id)
            .order_by(SubscriptionSource.id)
        )).scalars())
        selection = await select_primary_subscription_source(
            db,
            subscription,
            sources=sources,
        )
        next_sync_at = (
            next_future_subscription_check_at(
                _automatic_projection(subscription),
                config,
                now,
            )
            if selection is not None
            else None
        )
        result.append({
            "subscription_id": str(subscription.id),
            "name": subscription.name,
            "current_schedule_mode": subscription.schedule_mode,
            "current_sync_enabled": subscription.sync_enabled,
            "selected_source_id": str(selection.source.id) if selection else None,
            "selected_provider": selection.source.source if selection else None,
            "selected_source_url": selection.normalized_url if selection else None,
            "selection_reason": selection.reason if selection else "no_eligible_source",
            "next_sync_at": next_sync_at.isoformat() if next_sync_at else None,
            "source_count": len(sources),
            "enabled_source_count": sum(1 for source in sources if source.is_enabled),
        })
    return result


async def apply_subscription_schedule_migration_entry(
    db: AsyncSession,
    entry: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one frozen snapshot entry with short, ordered row locks."""

    now = now or datetime.now(timezone.utc)
    subscription_id = UUID(str(entry["subscription_id"]))
    audit_key = f"subscription_auto_migration:{subscription_id}"
    if (await db.execute(
        select(MaintenanceAuditEvent.id).where(
            MaintenanceAuditEvent.idempotency_key == audit_key
        )
    )).scalar_one_or_none() is not None:
        return {"subscription_id": str(subscription_id), "status": "already_applied"}

    subscription = (await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription_id)
        .with_for_update()
    )).scalar_one_or_none()
    if subscription is None:
        return {"subscription_id": str(subscription_id), "status": "missing_subscription"}
    sources = await lock_subscription_sources(db, subscription_id)
    selected_id_raw = entry.get("selected_source_id")
    if not selected_id_raw:
        return {"subscription_id": str(subscription_id), "status": "no_eligible_source"}
    selected_id = UUID(str(selected_id_raw))
    selected = next((source for source in sources if source.id == selected_id), None)
    if selected is None:
        return {"subscription_id": str(subscription_id), "status": "selected_source_missing"}
    valid, normalized = eligible_source(selected)
    if not valid or normalized is None:
        return {"subscription_id": str(subscription_id), "status": "selected_source_invalid"}

    subscription.schedule_mode = None
    subscription.sync_enabled = True
    config = await get_scheduler_config(db)
    next_sync_at = next_future_subscription_check_at(
        _automatic_projection(subscription),
        config,
        now,
    )
    for source in sources:
        source.is_enabled = source.id == selected.id
        source.next_sync_at = next_sync_at if source.id == selected.id else None
    selected.source_url = normalized
    summary = {
        "subscription_id": str(subscription.id),
        "name": subscription.name,
        "selected_source_id": str(selected.id),
        "selected_provider": selected.source,
        "selection_reason": entry.get("selection_reason"),
        "next_sync_at": next_sync_at.isoformat() if next_sync_at else None,
        "disabled_source_ids": [str(source.id) for source in sources if source.id != selected.id],
    }
    await db.execute(
        insert(MaintenanceAuditEvent)
        .values(
            event_type="subscription_auto_migration",
            idempotency_key=audit_key,
            summary=summary,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    await request_search_projection(
        db,
        creator_ids=[subscription.creator_id],
        repository_ids=[source.id for source in sources],
        subscription_ids=[subscription.id],
    )
    await db.flush()
    return {"subscription_id": str(subscription_id), "status": "applied", **summary}
