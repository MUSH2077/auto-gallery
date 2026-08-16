from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text


async def _clear(db):
    await db.execute(text("""
        TRUNCATE
            repository_sync_receipts,
            search_index_states,
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


async def _repository_fixture(db, *, download_status="complete"):
    from app.models import Creator, DownloadJob, Subscription, SubscriptionSource

    creator = Creator(name="receipt-test")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name="Receipt test")
    db.add(subscription)
    await db.flush()
    repository = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_creator_id="123",
        source_url="https://www.pixiv.net/users/123",
    )
    db.add(repository)
    await db.flush()
    download = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=repository.id,
        source="pixiv",
        source_url=repository.source_url,
        status=download_status,
        manifest={
            "outcome": {"code": "new_content", "metadata_count": 2, "media_count": 3},
            "import_stats": {"works": 2},
        },
    )
    db.add(download)
    await db.flush()
    return repository, download


@pytest.mark.integration
@pytest.mark.asyncio
async def test_receipt_is_idempotent_and_guards_task_compaction():
    from app.database import async_session, engine
    from app.models import DownloadJob, RepositorySyncReceipt, TaskRun
    from app.services.operation_attention import (
        compact_terminal_tasks,
        upsert_repository_sync_receipt,
    )
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            repository, download = await _repository_fixture(db)
            task = await TaskService(db).ensure_download_task(download)
            task.compactable_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await upsert_repository_sync_receipt(db, download)
            await upsert_repository_sync_receipt(db, download)
            await db.commit()

            receipt_count = int((await db.execute(select(func.count(RepositorySyncReceipt.id)))).scalar_one())
            assert receipt_count == 1
            receipt = (await db.execute(select(RepositorySyncReceipt))).scalar_one()
            assert receipt.repository_id == repository.id
            assert receipt.works_imported == 2

            report = await compact_terminal_tasks(db, dry_run=False)
            assert report["deleted_tasks"] == 1
            assert report["deleted_download_jobs"] == 1
            assert (await db.execute(select(TaskRun.id).where(TaskRun.id == task.id))).scalar_one_or_none() is None
            assert (await db.execute(select(DownloadJob.id).where(DownloadJob.id == download.id))).scalar_one_or_none() is None
            assert await db.get(RepositorySyncReceipt, receipt.id) is not None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compaction_never_deletes_repository_job_without_receipt():
    from app.database import async_session, engine
    from app.models import DownloadJob, TaskRun
    from app.services.operation_attention import compact_terminal_tasks
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            _repository, download = await _repository_fixture(db)
            task = await TaskService(db).ensure_download_task(download)
            task.compactable_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

            report = await compact_terminal_tasks(db, dry_run=False)
            assert report["skipped_without_receipt"] == 1
            assert await db.get(TaskRun, task.id) is not None
            assert await db.get(DownloadJob, download.id) is not None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_child_cannot_starve_compactable_parent_batch():
    from app.database import async_session, engine
    from app.models import DownloadJob, ImportJob, TaskRun
    from app.services.operation_attention import (
        compact_terminal_tasks,
        upsert_repository_sync_receipt,
    )
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            _repository, download = await _repository_fixture(db)
            parent = await TaskService(db).ensure_download_task(download)
            child_job = ImportJob(download_job_id=download.id, status="complete")
            db.add(child_job)
            await db.flush()
            child = await TaskService(db).ensure_import_task(
                child_job,
                parent_task_id=parent.id,
            )
            now = datetime.now(timezone.utc)
            child.compactable_at = now - timedelta(seconds=2)
            parent.compactable_at = now - timedelta(seconds=1)
            await upsert_repository_sync_receipt(db, download)
            await db.commit()

            report = await compact_terminal_tasks(db, dry_run=False, limit=1)
            assert report["deleted_tasks"] == 2
            assert report["deleted_import_jobs"] == 1
            assert (await db.execute(select(TaskRun.id).where(TaskRun.id == child.id))).scalar_one_or_none() is None
            assert (await db.execute(select(TaskRun.id).where(TaskRun.id == parent.id))).scalar_one_or_none() is None
            assert (await db.execute(select(DownloadJob.id).where(DownloadJob.id == download.id))).scalar_one_or_none() is None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_propagates_stale_child_to_parent_once():
    from app.database import async_session, engine
    from app.models import ImportJob, TaskEvent
    from app.services.operation_attention import reconcile_task_truth
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            _repository, download = await _repository_fixture(db, download_status="importing")
            parent = await TaskService(db).ensure_download_task(download)
            child_job = ImportJob(download_job_id=download.id, status="stale", error_log="heartbeat expired")
            db.add(child_job)
            await db.flush()
            await TaskService(db).ensure_import_task(child_job, parent_task_id=parent.id)
            await db.commit()

            first = await reconcile_task_truth(db, dry_run=False)
            second = await reconcile_task_truth(db, dry_run=False)
            await db.refresh(parent)
            assert first["parent_child_conflicts"] == 1
            assert first["corrected"] == 1
            assert second["corrected"] == 0
            assert parent.status == "stale"
            assert parent.attention_state == "open"
            assert parent.reason_code == "lost_heartbeat"
            event_count = int((await db.execute(
                select(func.count(TaskEvent.id)).where(
                    TaskEvent.task_run_id == parent.id,
                    TaskEvent.event_type == "reconciled",
                )
            )).scalar_one())
            assert event_count == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_later_success_resolves_historical_stale_and_marks_receipt_recovered():
    from app.database import async_session, engine
    from app.models import DownloadJob, RepositorySyncReceipt
    from app.services.operation_attention import (
        reconcile_task_truth,
        upsert_repository_sync_receipt,
    )
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            repository, stale_download = await _repository_fixture(
                db,
                download_status="stale",
            )
            stale_download.error_log = "heartbeat expired"
            stale_task = await TaskService(db).ensure_download_task(stale_download)
            stale_task.finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
            stale_task.updated_at = stale_task.finished_at
            stale_receipt = await upsert_repository_sync_receipt(
                db,
                stale_download,
                status="stale",
            )

            successful = DownloadJob(
                subscription_id=stale_download.subscription_id,
                subscription_source_id=repository.id,
                source=stale_download.source,
                source_url=stale_download.source_url,
                status="complete",
            )
            db.add(successful)
            await db.flush()
            success_task = await TaskService(db).ensure_download_task(successful)
            success_task.finished_at = datetime.now(timezone.utc)
            success_task.updated_at = success_task.finished_at
            await upsert_repository_sync_receipt(db, successful, status="complete")
            await db.commit()

            first = await reconcile_task_truth(db, dry_run=False)
            second = await reconcile_task_truth(db, dry_run=False)
            await db.refresh(stale_task)
            await db.refresh(stale_receipt)

            assert first["recovered"] == 1
            assert second["recovered"] == 0
            assert stale_task.status == "stale"
            assert stale_task.attention_state == "resolved"
            assert stale_task.compactable_at >= datetime.now(timezone.utc) + timedelta(days=6)
            assert stale_receipt.recovered is True
            assert stale_receipt.recovered_at is not None
            assert int((await db.execute(select(func.count(RepositorySyncReceipt.id)))).scalar_one()) == 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_later_import_retry_under_same_download_resolves_stale_child():
    from app.database import async_session, engine
    from app.models import ImportJob
    from app.services.operation_attention import (
        reconcile_task_truth,
        upsert_repository_sync_receipt,
    )
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            _repository, download = await _repository_fixture(
                db,
                download_status="complete",
            )
            parent = await TaskService(db).ensure_download_task(download)
            baseline = datetime.now(timezone.utc) - timedelta(hours=2)

            stale_import = ImportJob(
                download_job_id=download.id,
                status="stale",
                error_log="heartbeat expired",
                created_at=baseline - timedelta(minutes=5),
            )
            db.add(stale_import)
            await db.flush()
            stale_task = await TaskService(db).ensure_import_task(
                stale_import,
                parent_task_id=parent.id,
            )
            stale_task.finished_at = baseline
            stale_task.updated_at = baseline

            successful_import = ImportJob(
                download_job_id=download.id,
                status="complete",
                created_at=baseline + timedelta(hours=1),
            )
            db.add(successful_import)
            await db.flush()
            await TaskService(db).ensure_import_task(
                successful_import,
                parent_task_id=parent.id,
            )
            receipt = await upsert_repository_sync_receipt(
                db,
                download,
                status="complete",
            )
            receipt.finished_at = datetime.now(timezone.utc)
            await db.commit()

            first = await reconcile_task_truth(db, dry_run=False)
            second = await reconcile_task_truth(db, dry_run=False)
            await db.refresh(stale_task)
            await db.refresh(receipt)

            assert first["recovered"] == 1
            assert second["recovered"] == 0
            assert stale_task.status == "stale"
            assert stale_task.attention_state == "resolved"
            assert receipt.recovered is True
            assert receipt.recovered_at is not None
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_import_pause_resume_cancel_updates_parent_and_both_task_runs_atomically():
    from app.database import async_session, engine
    from app.models import ImportJob, RepositorySyncReceipt
    from app.services.import_lifecycle import project_import_pipeline_state
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            _repository, download = await _repository_fixture(
                db,
                download_status="importing",
            )
            parent_task = await TaskService(db).ensure_download_task(download)
            import_job = ImportJob(download_job_id=download.id, status="running")
            db.add(import_job)
            await db.flush()
            child_task = await TaskService(db).ensure_import_task(
                import_job,
                parent_task_id=parent_task.id,
            )

            import_job.status = "paused"
            await project_import_pipeline_state(db, import_job, status="paused")
            assert download.status == "paused"
            assert parent_task.status == "paused"
            assert child_task.status == "paused"

            import_job.status = "enqueued"
            await project_import_pipeline_state(db, import_job, status="enqueued")
            assert download.status == "importing"
            assert parent_task.status == "running"
            assert child_task.status == "enqueued"

            import_job.status = "cancelled"
            await project_import_pipeline_state(db, import_job, status="cancelled")
            await db.commit()
            assert download.status == "cancelled"
            assert parent_task.status == "cancelled"
            assert child_task.status == "cancelled"
            assert parent_task.attention_state == "none"
            assert child_task.attention_state == "none"
            assert parent_task.compactable_at is not None
            assert child_task.compactable_at is not None
            assert int((await db.execute(select(func.count(RepositorySyncReceipt.id)))).scalar_one()) == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unhealthy_repository_auth_appears_without_creating_task_run():
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.operation_attention import operations_overview

    try:
        async with async_session() as db:
            await _clear(db)
            repository, _download = await _repository_fixture(db)
            repository.auth_healthy = False
            repository.auth_status = "failed"
            repository.auth_error_reason = "cookie expired"
            await db.commit()

            overview = await operations_overview(db, view="attention")
            assert overview["summary"]["attention"] == 1
            assert overview["summary"]["critical"] == 1
            assert overview["items"][0]["reason_code"] == "auth_unhealthy"
            assert overview["items"][0]["repository_id"] == str(repository.id)
            assert overview["items"][0]["task"] is None
            assert int(
                (await db.execute(select(func.count(TaskRun.id)))).scalar_one()
            ) == 0
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acknowledged_anomaly_remains_in_resolved_window():
    from app.database import async_session, engine
    from app.services.operation_attention import operations_overview
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear(db)
            _repository, download = await _repository_fixture(db, download_status="failed")
            task = await TaskService(db).ensure_download_task(download)
            await TaskService(db).update_task(task, attention_state="acknowledged")
            await db.commit()

            attention = await operations_overview(db, view="attention")
            resolved = await operations_overview(db, view="resolved")
            assert attention["total"] == 0
            assert resolved["total"] == 1
            assert resolved["summary"]["resolved"] == 1
            assert resolved["items"][0]["status"] == "acknowledged"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
