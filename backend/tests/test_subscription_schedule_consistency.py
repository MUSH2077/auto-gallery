from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text


async def _clear(db):
    await db.execute(text("""
        TRUNCATE
            maintenance_audit_events,
            search_projection_outbox,
            repository_sync_receipts,
            task_events,
            task_runs,
            import_jobs,
            download_jobs,
            subscription_sources,
            subscriptions,
            source_creators,
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


async def _manual_fixture(db, suffix: str = "one"):
    from app.models import Creator, Subscription, SubscriptionSource

    creator = Creator(name=f"legacy-manual-{suffix}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name=f"Legacy {suffix}",
        schedule_mode="manual",
        sync_enabled=False,
    )
    db.add(subscription)
    await db.flush()
    pixiv = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_creator_id="12539859",
        source_url="https://www.pixiv.net/users/12539859",
        is_enabled=False,
    )
    x_source = SubscriptionSource(
        subscription_id=subscription.id,
        source="x",
        source_creator_id="ruzhaiii",
        source_url="https://x.com/ruzhaiii",
        is_enabled=False,
    )
    db.add_all([pixiv, x_source])
    await db.flush()
    return subscription, pixiv, x_source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_inherit_clears_manual_and_activates_one_primary_source():
    from app.database import async_session, engine
    from app.models import Subscription, SubscriptionSource
    from app.services.subscription import SubscriptionService

    try:
        async with async_session() as db:
            await _clear(db)
            subscription, pixiv, x_source = await _manual_fixture(db)
            subscription_id = subscription.id
            pixiv_id = pixiv.id
            x_id = x_source.id
            await db.commit()

            updated = await SubscriptionService(db).update_subscription(
                subscription_id,
                {"schedule_mode": "inherit"},
            )

            assert updated.schedule_mode is None
            assert updated.sync_enabled is True
            assert updated.configured_mode == "inherit"
            assert updated.auto_enabled_source["id"] == pixiv_id
            assert updated.next_sync_at is not None
            persisted = await db.get(Subscription, subscription_id)
            assert persisted.schedule_mode is None
            sources = {
                source.id: source
                for source in (await db.execute(
                    select(SubscriptionSource).where(
                        SubscriptionSource.subscription_id == subscription_id
                    )
                )).scalars()
            }
            assert sources[pixiv_id].is_enabled is True
            assert sources[pixiv_id].next_sync_at is not None
            assert sources[x_id].is_enabled is False
            assert sources[x_id].next_sync_at is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unset_schedule_field_does_not_clear_manual_mode():
    from app.database import async_session, engine
    from app.services.subscription import SubscriptionService

    try:
        async with async_session() as db:
            await _clear(db)
            subscription, _pixiv, _x_source = await _manual_fixture(db, "unchanged")
            subscription_id = subscription.id
            await db.commit()

            updated = await SubscriptionService(db).update_subscription(
                subscription_id,
                {"name": "Renamed"},
            )

            assert updated.name == "Renamed"
            assert updated.schedule_mode == "manual"
            assert updated.sync_enabled is False
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_migration_is_frozen_and_idempotent():
    from app.database import async_session, engine
    from app.models import MaintenanceAuditEvent, SubscriptionSource
    from app.services.subscription_schedule_repair import (
        apply_subscription_schedule_migration_entry,
        build_subscription_schedule_migration_plan,
    )

    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    try:
        async with async_session() as db:
            await _clear(db)
            subscription, pixiv, _x_source = await _manual_fixture(db, "migration")
            subscription_id = subscription.id
            pixiv_id = pixiv.id
            await db.commit()
            plan = await build_subscription_schedule_migration_plan(
                db,
                subscription_ids=[subscription_id],
                now=now,
            )
            assert len(plan) == 1
            assert plan[0]["selected_source_id"] == str(pixiv_id)
            assert plan[0]["selected_provider"] == "pixiv"
            assert plan[0]["next_sync_at"] is not None

            applied = await apply_subscription_schedule_migration_entry(
                db,
                plan[0],
                now=now,
            )
            await db.commit()
            repeated = await apply_subscription_schedule_migration_entry(
                db,
                plan[0],
                now=now,
            )
            await db.rollback()

            assert applied["status"] == "applied"
            assert repeated["status"] == "already_applied"
            assert int((await db.execute(
                select(func.count(MaintenanceAuditEvent.id)).where(
                    MaintenanceAuditEvent.event_type == "subscription_auto_migration"
                )
            )).scalar_one()) == 1
            enabled = list((await db.execute(
                select(SubscriptionSource).where(
                    SubscriptionSource.subscription_id == subscription_id,
                    SubscriptionSource.is_enabled.is_(True),
                )
            )).scalars())
            assert [source.id for source in enabled] == [pixiv_id]
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
