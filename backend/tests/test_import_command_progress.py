"""The import command commits facts and presentation snapshots together."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_import_lifecycle_batches import _clear, _shared_parent


@pytest.mark.integration
@pytest.mark.parametrize("status,message", [
    ("failed", "No parseable metadata"),
    ("failed", "Import failed after all retries"),
    ("paused", "Paused after 2 works"),
    ("cancelled", "Cancelled after 2 works"),
    ("enqueued", "Retry 2/3 queued after import error"),
])
async def test_import_command_projects_current_progress(monkeypatch, status, message):
    from app.database import async_session, engine
    from app.models import ImportJob, TaskRun, DownloadJob
    from app.services.import_lifecycle import apply_import_command
    from app.services import job_progress

    monkeypatch.setattr(job_progress, "publish_progress", lambda *a, **k: None)
    token = uuid4()
    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db, manifest={})
            child = ImportJob(download_job_id=parent.id, status="failed" if status == "enqueued" else "running", execution_token=token,
                              progress_data={"stage": "importing", "message": "old progress"})
            db.add(child)
            await db.commit()
            child_id, parent_id = child.id, parent.id
        async with async_session() as db:
            await apply_import_command(db, child_id, status, execution_token=token,
                                       message=message, progress={"stage": status, "message": message, "current": 2, "total": 5},
                                       retry_count=2, result={"reason": "fixture"})
            await db.commit()
        async with async_session() as db:
            child = await db.get(ImportJob, child_id)
            tasks = list((await db.execute(select(TaskRun).where(TaskRun.subject_id.in_([child_id, parent_id])))).scalars())
            child_task = next(row for row in tasks if row.subject_id == child_id)
            parent_task = next(row for row in tasks if row.subject_id == parent_id)
            parent = await db.get(DownloadJob, parent_id)
            assert child_task.progress_data == child.progress_data
            assert child.progress_data["message"] == parent_task.progress_data["message"] == parent.progress_data["message"] == message
            assert child_task.result_data == {"reason": "fixture"}
            assert child.import_retry_count == 2
            assert child.progress_works_done == 2
        async with async_session() as db:
            assert await apply_import_command(db, child_id, "failed", execution_token=uuid4(),
                                              progress={"stage": "failed", "message": "old owner"}, retry_count=99) is None
            await db.commit()
        async with async_session() as db:
            child = await db.get(ImportJob, child_id)
            assert child.progress_data["message"] == message
            assert child.import_retry_count == 2
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("status,stats", [
    ("complete", {"works": 0, "assets": 0, "multi_page": 0, "existing": 775, "skipped": 5}),
    ("failed", {"works": 1, "assets": 2, "multi_page": 1, "existing": 1, "skipped": 3}),
])
async def test_terminal_import_keeps_processed_count_and_stats(monkeypatch, status, stats):
    from app.database import async_session, engine
    from app.models import ImportJob, TaskRun
    from app.services.import_lifecycle import complete_import_command
    from app.services.tasks import TaskService
    from app.services import job_progress

    monkeypatch.setattr(job_progress, "publish_progress", lambda *a, **k: None)
    token = uuid4()
    total = stats["works"] + stats["existing"] + stats["skipped"]
    message = "Import complete with skipped works" if status == "complete" else "High import skip rate"
    try:
        async with async_session() as db:
            await _clear(db)
            parent = await _shared_parent(db)
            child = ImportJob(download_job_id=parent.id, status="running", execution_token=token)
            db.add(child)
            await db.flush()
            await TaskService(db).ensure_import_task(child)
            child_id = child.id
            await db.commit()
        async with async_session() as db:
            _, progress = await complete_import_command(
                db, child_id, token, status=status, message=message, stats=stats, total_groups=total,
            )
            assert progress["current"] == progress["total"] == total
            await db.commit()
        async with async_session() as db:
            child = await db.get(ImportJob, child_id)
            task = (await db.execute(select(TaskRun).where(
                TaskRun.subject_type == "import_job", TaskRun.subject_id == child_id,
            ))).scalar_one()
            assert child.status == task.status == status
            assert child.progress_works_done == child.progress_works_total == total
            assert task.progress_data == child.progress_data
            assert task.result_data == {"stats": stats, "message": message, "total_groups": total}
            assert child.error_log == (message if status == "failed" else None)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
