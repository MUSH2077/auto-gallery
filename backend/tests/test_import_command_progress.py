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
