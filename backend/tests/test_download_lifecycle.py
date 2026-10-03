"""Real transaction evidence for the explicit download lifecycle owner."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_import_lifecycle_batches import _clear, _shared_parent

pytestmark = pytest.mark.integration


@pytest.fixture
async def domain():
    from app.database import async_session, engine
    from app.models import SubscriptionSource
    async with async_session() as db:
        await _clear(db)
        job = await _shared_parent(db, manifest={"trigger": "fixture"})
        source = SubscriptionSource(subscription_id=job.subscription_id, source=job.source, source_url=job.source_url, source_creator_id="1980643")
        db.add(source)
        await db.flush()
        job.subscription_source_id = source.id
        job.status = "enqueued"
        await db.commit()
        identity = job.id
    yield identity
    async with async_session() as db:
        await _clear(db)
    await engine.dispose()


async def test_repository_does_not_create_or_update_task_projection(domain):
    from app.database import async_session
    from app.models import TaskRun
    from app.repositories.download_job import DownloadJobRepository
    async with async_session() as db:
        repo = DownloadJobRepository(db)
        parent = await repo.get(domain)
        child = await repo.create_import({"download_job_id": domain, "status": "enqueued"})
        await repo.update_status(parent, "downloading")
        await db.commit()
        assert not list((await db.execute(select(TaskRun).where(TaskRun.subject_id.in_([domain, child.id])))).scalars())
        from app.services.tasks import TaskService
        projection = await TaskService(db).ensure_download_task(parent)
        await db.commit()
        await repo.update_status(parent, "failed", "repository-only change")
        await db.commit()
        await db.refresh(projection)
        assert projection.status == "running"
        assert projection.error_log is None


async def test_control_notification_follows_the_authoritative_commit(domain, monkeypatch):
    from app.database import async_session
    from app.models import DownloadJob
    from app.services import task_engine
    events = []
    async with async_session() as db:
        original_commit = db.commit
        async def commit():
            await original_commit()
            events.append("commit")
        monkeypatch.setattr(db, "commit", commit)
        def notification(*args, **kwargs):
            assert events == ["commit"]
        monkeypatch.setattr(task_engine.TaskEventPublisher, "send_control", notification)
        monkeypatch.setattr(task_engine.TaskEventPublisher, "publish_status_change", notification)
        await task_engine.TaskEngine(db).cancel_download(domain)
    async with async_session() as db:
        assert (await db.get(DownloadJob, domain)).status == "cancelled"


async def test_failed_state_projection_and_receipt_roll_back_together(domain):
    from app.database import async_session
    from app.models import DownloadJob, TaskRun
    from app.models.repository_sync_receipt import RepositorySyncReceipt
    from app.services.download_lifecycle import claim_download, apply_download_state, lock_download
    async with async_session() as db:
        job, execution = await claim_download(db, domain, "first-attempt")
        await db.commit()
    async with async_session() as db:
        job = await lock_download(db, domain, execution=execution)
        await apply_download_state(db, job, "failed", error="failure", progress={"stage": "failed", "message": "failed progress"}, retry_count=2)
        await db.rollback()
    async with async_session() as db:
        job = await db.get(DownloadJob, domain)
        task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == domain))).scalar_one()
        assert job.status == "downloading" and task.status == "running"
        assert task.progress_data == job.progress_data
        assert (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == domain))).scalar_one_or_none() is None
        job = await lock_download(db, domain, execution=execution)
        await apply_download_state(db, job, "failed", error="failure", progress={"stage": "failed", "message": "failed progress"}, retry_count=2)
        await db.commit()
    async with async_session() as db:
        job = await db.get(DownloadJob, domain)
        task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == domain))).scalar_one()
        receipt = (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == domain))).scalar_one()
        assert job.status == task.status == receipt.status == "failed"
        assert job.progress_data == task.progress_data
        assert task.meta["retry_count"] == job.retry_count == 2


async def test_retry_fences_old_execution_and_cancel_fences_completion(domain, monkeypatch):
    from types import SimpleNamespace
    from app.database import async_session
    from app.models import DownloadJob, TaskRun
    from app.services.download_lifecycle import claim_download, apply_download_state, lock_download
    from app.services.download_finalization import finalize_download_job
    from app.services.task_engine import TaskEngine
    from app.services import download_dispatch
    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", lambda *a, rq_job_id, **k: SimpleNamespace(id=rq_job_id))
    async with async_session() as db:
        job, old = await claim_download(db, domain, "first-attempt")
        await apply_download_state(db, job, "failed", error="retryable", progress={"stage": "failed", "message": "old attempt"})
        await db.commit()
    async with async_session() as db:
        await TaskEngine(db).retry_download(domain)
    async with async_session() as db:
        assert await lock_download(db, domain, execution=old) is None
        job = await db.get(DownloadJob, domain)
        with pytest.raises(RuntimeError, match="no longer owns"):
            await finalize_download_job(db, job, status="complete", execution=old)
        await db.rollback()
    async with async_session() as db:
        task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == domain))).scalar_one()
        job, current = await claim_download(db, domain, task.rq_job_id)
        assert job.retry_count == 0
        await db.commit()
    async with async_session() as db:
        await TaskEngine(db).cancel_download(domain)
    async with async_session() as db:
        job = await db.get(DownloadJob, domain)
        with pytest.raises(RuntimeError, match="Cancelled"):
            await finalize_download_job(db, job, status="complete", execution=current)
        await db.rollback()
    async with async_session() as db:
        task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == domain))).scalar_one()
        assert task.status == "cancelled"


@pytest.mark.parametrize("staged_reconciliation", [False, True])
async def test_cancellation_wins_over_a_previously_loaded_finalizer(domain, staged_reconciliation):
    from app.database import async_session
    from app.models import DownloadJob, TaskRun
    from app.services.download_lifecycle import claim_download, apply_download_state, lock_download
    from app.services.download_finalization import finalize_download_job
    from app.services.task_engine import TaskEngine
    async with async_session() as db:
        job, execution = await claim_download(db, domain, "attempt-one")
        await apply_download_state(db, job, "downloaded")
        await db.commit()
    async with async_session() as old:
        job = await old.get(DownloadJob, domain)
        job.progress_data = {"stage": "complete", "message": "late data"}
        async with async_session() as control:
            await TaskEngine(control).cancel_download(domain)
        if staged_reconciliation:
            assert await lock_download(old, domain, execution=execution, statuses={"downloaded"}, refresh=False) is None
        else:
            with pytest.raises(RuntimeError, match="Cancelled"):
                await finalize_download_job(old, job, status="complete", execution=execution)
        await old.rollback()
    async with async_session() as db:
        job = await db.get(DownloadJob, domain)
        task = (await db.execute(select(TaskRun).where(TaskRun.subject_id == domain))).scalar_one()
        assert job.status == task.status == "cancelled"
        assert job.progress_data.get("message") != "late data"


async def test_concurrent_retry_rechecks_eligibility_after_lock(domain, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.database import async_session
    from app.services import download_dispatch, download_lifecycle
    from app.services.task_engine import TaskEngine
    from app.services import task_engine
    from fastapi import HTTPException
    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", lambda *a, rq_job_id, **k: SimpleNamespace(id=rq_job_id))
    async with async_session() as db:
        job, _ = await download_lifecycle.claim_download(db, domain, "first")
        await download_lifecycle.apply_download_state(db, job, "failed", error="fixture")
        await db.commit()
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = task_engine.require_job_action
    async with async_session() as old:
        async def gated(db, *args, **kwargs):
            if db is old:
                entered.set()
                await proceed.wait()
            return await original(db, *args, **kwargs)
        monkeypatch.setattr(task_engine, "require_job_action", gated)
        pending = asyncio.create_task(TaskEngine(old).retry_download(domain))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with async_session() as winner:
                await asyncio.wait_for(TaskEngine(winner).retry_download(domain), 5)
            proceed.set()
            with pytest.raises(HTTPException) as rejected:
                await asyncio.wait_for(pending, 5)
            assert rejected.value.status_code == 409
        finally:
            proceed.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
