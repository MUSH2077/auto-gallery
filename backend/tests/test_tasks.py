from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text


def test_normalize_task_status_maps_legacy_queued_states():
    from app.services.tasks import normalize_task_status

    assert normalize_task_status("queued") == "enqueued"
    assert normalize_task_status("pending") == "enqueued"
    assert normalize_task_status("downloaded") == "running"
    assert normalize_task_status("importing") == "running"
    assert normalize_task_status("complete") == "complete"


async def _clear_task_test_tables(db):
    await db.execute(text("""
        TRUNCATE
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_task_service_creates_listable_admin_task_with_events():
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)
            task = await svc.create_task(
                kind="admin",
                operation_type="admin-disk-import",
                title="Import from disk",
                status="queued",
                queue_name="operations",
                progress={"phase": "queued", "label": "Queued"},
            )
            await db.commit()

            total, tasks = await svc.list_tasks(kind="admin", status="queued")
            events = await svc.task_events(task.id)

            assert total == 1
            assert tasks[0].id == task.id
            assert tasks[0].status == "enqueued"
            assert tasks[0].operation_type == "admin-disk-import"
            assert events[0].event_type == "created"
            assert events[0].to_status == "enqueued"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_download_and_import_jobs_sync_to_parent_child_task_runs():
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.subscription import Subscription
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            creator = Creator(name="task-sync", display_name="Task Sync")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="Task Sync")
            db.add(sub)
            await db.flush()
            download = DownloadJob(
                subscription_id=sub.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/123",
                status="downloaded",
                progress_data={"stage": "downloaded", "current": 2, "total": 2},
                updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            db.add(download)
            await db.flush()
            import_job = ImportJob(
                download_job_id=download.id,
                status="running",
                progress_data={"stage": "importing", "current": 1, "total": 2},
            )
            db.add(import_job)
            await db.flush()

            svc = TaskService(db)
            download_task = await svc.ensure_download_task(download)
            import_task = await svc.ensure_import_task(import_job)
            await db.commit()

            assert download_task.kind == "download"
            assert download_task.status == "running"
            assert import_task.kind == "import"
            assert import_task.status == "running"
            assert import_task.parent_task_id == download_task.id

            total, tasks = await svc.list_tasks(q="pixiv")
            assert total >= 1
            assert any(task.id == download_task.id for task in tasks)
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()
