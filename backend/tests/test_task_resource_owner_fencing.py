from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text


async def _clear_tables(db) -> None:
    await db.execute(
        text(
            "TRUNCATE task_events, task_runs, import_jobs, download_jobs, "
            "subscriptions, creators RESTART IDENTITY CASCADE"
        )
    )
    await db.commit()


async def _seed_import_task(db):
    from app.models import Creator, DownloadJob, ImportJob, Subscription
    from app.services.tasks import TaskService

    creator = Creator(name=f"resource-owner-{uuid4().hex}")
    db.add(creator)
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name="Resource owner")
    db.add(subscription)
    await db.flush()
    download = DownloadJob(
        subscription_id=subscription.id,
        source="pixiv",
        source_url="https://www.pixiv.net/users/123",
        status="importing",
    )
    db.add(download)
    await db.flush()
    execution_token = uuid4()
    import_job = ImportJob(
        download_job_id=download.id,
        status="running",
        execution_token=execution_token,
        execution_attempt=1,
    )
    db.add(import_job)
    await db.flush()
    task = await TaskService(db).ensure_import_task(import_job)
    task.resource_state = "waiting"
    task.resource_reason = None
    await db.commit()
    return import_job.id, execution_token, task.id


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_valid_composite_import_owner_updates_actual_task_run(explicit):
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.tasks import update_task_resource_state

    try:
        async with async_session() as db:
            await _clear_tables(db)
            import_id, execution_token, task_id = await _seed_import_task(db)

        await update_task_resource_state(
            str(import_id) if explicit else f"{import_id}:{execution_token}",
            "running",
            "import_db",
            **({"execution_token": str(execution_token)} if explicit else {}),
        )

        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.resource_state == "running"
            assert task.resource_reason == "import_db"
    finally:
        async with async_session() as db:
            await _clear_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_factory",
    [
        lambda import_id, _token: str(import_id),
        lambda import_id, _token: f"{import_id}:{uuid4()}",
        lambda import_id, _token: f"{import_id}:not-a-token",
        lambda import_id, token: f"{import_id}:{token}:extra",
        lambda _import_id, token: f"not-an-import:{token}",
    ],
)
async def test_stale_or_malformed_composite_import_owner_is_rejected(owner_factory):
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.tasks import update_task_resource_state

    try:
        async with async_session() as db:
            await _clear_tables(db)
            import_id, execution_token, task_id = await _seed_import_task(db)

        await update_task_resource_state(
            owner_factory(import_id, execution_token),
            "running",
            "must-not-apply",
        )

        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.resource_state == "waiting"
            assert task.resource_reason is None
    finally:
        async with async_session() as db:
            await _clear_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plain_import_task_uuid_cannot_bypass_execution_fencing():
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.tasks import update_task_resource_state

    try:
        async with async_session() as db:
            await _clear_tables(db)
            _import_id, _execution_token, task_id = await _seed_import_task(db)

        await update_task_resource_state(str(task_id), "yielded", "checkpoint")

        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.resource_state == "waiting"
            assert task.resource_reason is None
    finally:
        async with async_session() as db:
            await _clear_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plain_download_task_uuid_remains_compatible():
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services.tasks import update_task_resource_state

    try:
        async with async_session() as db:
            await _clear_tables(db)
            _import_id, _execution_token, task_id = await _seed_import_task(db)
            import_task = await db.get(TaskRun, task_id)
            download_task_id = import_task.parent_task_id
            if download_task_id is None:
                from app.models import DownloadJob, ImportJob
                from app.services.tasks import TaskService

                child = await db.get(ImportJob, _import_id)
                parent = await db.get(DownloadJob, child.download_job_id)
                task = await TaskService(db).ensure_download_task(parent)
                download_task_id = task.id
                await db.commit()
        await update_task_resource_state(str(download_task_id), "yielded", "checkpoint")
        async with async_session() as db:
            task = await db.get(TaskRun, download_task_id)
            assert task.resource_state == "yielded"
            assert task.resource_reason == "checkpoint"
    finally:
        async with async_session() as db:
            await _clear_tables(db)
        await engine.dispose()
