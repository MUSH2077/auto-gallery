from datetime import datetime, timedelta, timezone
from uuid import uuid4

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
            creators
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


async def _subscription_fixture(db, *, suffix: str = "one"):
    from app.models import Creator, Subscription, SubscriptionSource

    creator = Creator(name=f"schedule-summary-{suffix}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name=f"Summary {suffix}",
        schedule_mode="fixed_time",
        scheduled_times="22:00",
        sync_enabled=True,
    )
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_creator_id=suffix,
        source_url=f"https://www.pixiv.net/users/{1000 + len(suffix)}",
        next_sync_at=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    db.add(source)
    await db.flush()
    return subscription, source


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_ignores_resolved_stale_when_newer_no_changes_receipt_exists():
    from app.database import async_session, engine
    from app.models import DownloadJob, RepositorySyncReceipt
    from app.services.subscription_summary import subscription_summaries
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            subscription, source = await _subscription_fixture(db)
            stale_download = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=source.id,
                source="pixiv",
                source_url=source.source_url,
                status="stale",
            )
            db.add(stale_download)
            await db.flush()
            stale_task = await TaskService(db).ensure_download_task(stale_download)
            stale_task.attention_state = "resolved"
            stale_task.resolved_at = datetime.now(timezone.utc) - timedelta(minutes=15)
            stale_task.updated_at = stale_task.resolved_at
            finished_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            receipt = RepositorySyncReceipt(
                repository_id=source.id,
                source_download_job_id=uuid4(),
                source="pixiv",
                status="complete",
                outcome_code="no_changes",
                finished_at=finished_at,
            )
            db.add(receipt)
            await db.commit()

            payload = await subscription_summaries(db, [subscription.id])
            summary = payload["items"][0]
            assert summary["latest_state"]["state"] == "success"
            assert summary["latest_state"]["outcome_code"] == "no_changes"
            assert summary["attention_count"] == 0
            assert summary["schedule"]["next_due_at"] is not None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missed_slot_restore_is_dry_run_safe_and_idempotent():
    from app.database import async_session, engine
    from app.models import MaintenanceAuditEvent, SubscriptionSource
    from app.services.operation_attention import restore_missed_subscription_slot

    slot = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)
    try:
        async with async_session() as db:
            await _clear(db)
            _subscription, missed = await _subscription_fixture(db, suffix="missed")
            missed.next_sync_at = slot + timedelta(days=1)
            missed.last_attempted_at = slot - timedelta(days=1)
            _subscription, attempted = await _subscription_fixture(db, suffix="attempted")
            attempted.next_sync_at = slot + timedelta(days=1)
            attempted.last_attempted_at = slot + timedelta(minutes=3)
            await db.commit()
            missed_id = missed.id
            attempted_id = attempted.id

            dry_run = await restore_missed_subscription_slot(
                db,
                slot_at=slot,
                source_ids=[missed_id, attempted_id],
                dry_run=True,
            )
            assert dry_run["matched"] == 1
            missed = await db.get(SubscriptionSource, missed_id)
            assert missed.next_sync_at == slot + timedelta(days=1)

            applied = await restore_missed_subscription_slot(
                db,
                slot_at=slot,
                source_ids=[missed_id, attempted_id],
                dry_run=False,
            )
            repeated = await restore_missed_subscription_slot(
                db,
                slot_at=slot,
                source_ids=[missed_id, attempted_id],
                dry_run=False,
            )
            missed = await db.get(SubscriptionSource, missed_id)
            attempted = await db.get(SubscriptionSource, attempted_id)
            assert applied["matched"] == 1
            assert repeated["matched"] == 0
            assert missed.next_sync_at == slot
            assert attempted.next_sync_at == slot + timedelta(days=1)
            audit_count = int((await db.execute(
                select(func.count(MaintenanceAuditEvent.id)).where(
                    MaintenanceAuditEvent.event_type == "subscription_slot_restore"
                )
            )).scalar_one())
            assert audit_count == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
