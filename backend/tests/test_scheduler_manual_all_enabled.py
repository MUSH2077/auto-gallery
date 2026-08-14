from __future__ import annotations

import pytest
from sqlalchemy import select, text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE task_events, task_runs, download_jobs, subscription_sources, "
        "subscriptions, creators RESTART IDENTITY CASCADE"
    ))
    await db.commit()


async def _seed_source(
    db,
    *,
    name: str,
    schedule_mode: str | None,
    sync_enabled: bool,
    subscription_active: bool = True,
    source_enabled: bool = True,
):
    from app.models import Creator, Subscription, SubscriptionSource

    creator = Creator(name=name, display_name=name)
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name=name,
        is_active=subscription_active,
        sync_enabled=sync_enabled,
        schedule_mode=schedule_mode,
        sync_interval_hours=9,
        scheduled_times="04:30",
    )
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_url=f"https://www.pixiv.net/users/{len(name) + 100}",
        source_creator_id=str(len(name) + 100),
        is_enabled=source_enabled,
    )
    db.add(source)
    await db.flush()
    return subscription, source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manual_all_enabled_includes_manual_and_preserves_strategy(monkeypatch):
    from app.api.admin.scheduler import SchedulerSyncNowRequest, trigger_sync_now
    from app.database import async_session, engine
    from app.models import Subscription, SubscriptionSource
    from app.services import subscription_enqueue

    enqueued_ids = []

    async def _enqueue(_db, source_id, **kwargs):
        enqueued_ids.append(source_id)
        return {
            "status": "enqueued",
            "source_id": str(source_id),
            "job_id": f"job-{source_id}",
            "task_id": f"task-{source_id}",
        }

    monkeypatch.setattr(subscription_enqueue, "enqueue_subscription_source_sync", _enqueue)

    try:
        async with async_session() as db:
            await _clear(db)
            automatic, automatic_source = await _seed_source(
                db,
                name="automatic",
                schedule_mode="interval",
                sync_enabled=True,
            )
            manual, manual_source = await _seed_source(
                db,
                name="manual",
                schedule_mode="manual",
                sync_enabled=False,
            )
            _, inactive_source = await _seed_source(
                db,
                name="inactive",
                schedule_mode="interval",
                sync_enabled=True,
                subscription_active=False,
            )
            _, disabled_source = await _seed_source(
                db,
                name="disabled",
                schedule_mode="interval",
                sync_enabled=True,
                source_enabled=False,
            )
            await db.commit()

            result = await trigger_sync_now(
                SchedulerSyncNowRequest(mode="manual_all_enabled"),
                db,
            )

            assert result["mode"] == "manual_all_enabled"
            assert result["candidate_count"] == 2
            assert result["enqueued_count"] == 2
            assert set(enqueued_ids) == {automatic_source.id, manual_source.id}
            assert inactive_source.id not in enqueued_ids
            assert disabled_source.id not in enqueued_ids

            refreshed = {
                item.id: item
                for item in (await db.execute(select(Subscription).where(
                    Subscription.id.in_([automatic.id, manual.id]),
                ))).scalars().all()
            }
            assert refreshed[automatic.id].schedule_mode == "interval"
            assert refreshed[automatic.id].sync_enabled is True
            assert refreshed[automatic.id].sync_interval_hours == 9
            assert refreshed[automatic.id].scheduled_times == "04:30"
            assert refreshed[manual.id].schedule_mode == "manual"
            assert refreshed[manual.id].sync_enabled is False
            assert refreshed[manual.id].sync_interval_hours == 9
            assert refreshed[manual.id].scheduled_times == "04:30"
            persisted_sources = {
                item.id: item
                for item in (await db.execute(select(SubscriptionSource).where(
                    SubscriptionSource.id.in_([automatic_source.id, manual_source.id]),
                ))).scalars().all()
            }
            assert persisted_sources[automatic_source.id].is_enabled is True
            assert persisted_sources[manual_source.id].is_enabled is True
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_force_eligible_keeps_excluding_explicit_manual(monkeypatch):
    from app.api.admin.scheduler import SchedulerSyncNowRequest, trigger_sync_now
    from app.database import async_session, engine
    from app.services import subscription_enqueue

    enqueued_ids = []

    async def _enqueue(_db, source_id, **kwargs):
        enqueued_ids.append(source_id)
        return {"status": "skipped", "skip_reason": "already_running", "source_id": str(source_id)}

    monkeypatch.setattr(subscription_enqueue, "enqueue_subscription_source_sync", _enqueue)

    try:
        async with async_session() as db:
            await _clear(db)
            _, automatic_source = await _seed_source(
                db,
                name="automatic",
                schedule_mode="interval",
                sync_enabled=True,
            )
            _, manual_source = await _seed_source(
                db,
                name="manual",
                schedule_mode="manual",
                sync_enabled=False,
            )
            await db.commit()

            result = await trigger_sync_now(
                SchedulerSyncNowRequest(mode="force_eligible"),
                db,
            )

            assert enqueued_ids == [automatic_source.id]
            assert manual_source.id not in enqueued_ids
            assert result["candidate_count"] == 1
            assert result["skipped_count"] == 1
            assert result["skipped_reasons"] == {"already_running": 1}
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
