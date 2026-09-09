"""Real worker regressions for durable retry ownership and partial artifacts."""

from datetime import datetime, timedelta, timezone
from io import StringIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text


@pytest.fixture
async def db():
    from app.database import async_session, engine

    async with async_session() as session:
        await session.execute(text(
            "TRUNCATE task_runs, creators, users RESTART IDENTITY CASCADE"
        ))
        await session.execute(text(
            "TRUNCATE scheduler_batch_items, scheduler_batches"
        ))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


async def seed(db):
    from app.models import Creator, Subscription, SubscriptionSource, User
    from app.models.remote_discovery import UserSubscription, UserSubscriptionSource

    user = User(username=f"retry-r1-{uuid4()}", password_hash="test-only", is_active=True)
    creator = Creator(name=f"retry-r1-{uuid4()}")
    db.add_all([user, creator])
    await db.flush()
    subscription = Subscription(
        creator_id=creator.id,
        name="retry-r1",
        is_active=True,
        sync_enabled=True,
        schedule_mode="interval",
    )
    db.add(subscription)
    await db.flush()
    membership = UserSubscription(
        user_id=user.id,
        subscription_id=subscription.id,
        is_active=True,
        sync_enabled=True,
        schedule_mode="interval",
    )
    db.add(membership)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_url="https://www.pixiv.net/users/1000",
        source_creator_id="1000",
        is_enabled=True,
        auth_healthy=True,
    )
    db.add(source)
    await db.flush()
    db.add(UserSubscriptionSource(
        user_id=user.id,
        subscription_id=subscription.id,
        user_subscription_id=membership.id,
        subscription_source_id=source.id,
        is_enabled=True,
        auth_healthy=True,
    ))
    await db.commit()


async def admit(db):
    from app.api.admin.scheduler import SchedulerSyncNowRequest, trigger_sync_now

    return await trigger_sync_now(SchedulerSyncNowRequest(mode="force_eligible"), db)


async def slice_batch(db, task_id):
    from app.models import TaskRun
    from app.services.operations import admin_operation_attempt_context
    from app.services.scheduler_batches import run_batch_slice

    await db.rollback()
    task = await db.get(TaskRun, UUID(task_id))
    attempt = task.attempts
    await db.rollback()
    with admin_operation_attempt_context(task_id, attempt):
        return await run_batch_slice(task_id, {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "capacity_code"),
    [
        ("unexpected", "queue_saturated"),
        ("unexpected", "enqueue_busy"),
        ("unexpected", "redis_unwritable"),
        ("nonzero", "queue_saturated"),
        ("nonzero", "enqueue_busy"),
        ("nonzero", "redis_unwritable"),
        ("timeout", "queue_saturated"),
    ],
)
async def test_worker_partial_failure_defers_retry_without_starting_import(
    db,
    monkeypatch,
    tmp_path,
    failure_kind,
    capacity_code,
):
    from app.jobs import download
    from app.models import (
        DownloadJob,
        ImportJob,
        RepositorySyncReceipt,
        StorageArtifact,
        TaskRun,
    )
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services import backpressure, download_dispatch, proxy, scheduler_batches

    async def no_pressure(*_args, **_kwargs):
        return None

    async def retain(*_args, **_kwargs):
        return None

    async def defaults():
        return {
            "max_retries": 4,
            "retry_backoff_base_seconds": 60,
            "timeout_seconds": -1 if failure_kind == "timeout" else 6000,
            "stall_timeout_seconds": 999,
        }

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(scheduler_batches, "_publish_bound_child", retain)
    await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    job_id, task_id = item.download_job_id, item.child_task_id
    artifact = StorageArtifact(
        storage_root="downloads",
        file_path=f"pixiv/r1/{failure_kind}.json",
        source="pixiv",
        creator_dir="r1",
        source_work_id=failure_kind,
        file_name=f"{failure_kind}.json",
        artifact_type="metadata_json",
        state="new",
        download_job_id=job_id,
    )
    media_artifact = StorageArtifact(
        storage_root="downloads",
        file_path=f"pixiv/r1/{failure_kind}.jpg",
        source="pixiv",
        creator_dir="r1",
        source_work_id=failure_kind,
        file_name=f"{failure_kind}.jpg",
        artifact_type="image",
        state="new",
        download_job_id=job_id,
    )
    db.add_all([artifact, media_artifact])
    seeded_job = await db.get(DownloadJob, job_id)
    seeded_job.source_url = "https://localhost/users/123"
    original_identity = (
        seeded_job.owner_user_id,
        seeded_job.triggering_user_subscription_id,
        seeded_job.subscription_source_id,
        item.batch_id,
    )
    await db.commit()

    monkeypatch.setattr(download, "_read_download_defaults", defaults)
    monkeypatch.setattr(download, "build_effective_gallerydl_config", lambda *_a: {})
    stage_calls = []

    def staging_failure():
        stage_calls.append(True)
        if len(stage_calls) > 1 and failure_kind == "unexpected":
            raise RuntimeError("injected execution setup failure")
        return False

    monkeypatch.setattr(download, "staging_enabled", staging_failure)
    monkeypatch.setattr(download.settings, "download_root", str(tmp_path))
    monkeypatch.setattr(download_dispatch, "_fetch_download_rq_job", lambda *_a, **_k: None)

    def refuse(*_args, **_kwargs):
        raise backpressure.DownloadAdmissionError(capacity_code, "temporary refusal")

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", refuse)
    monkeypatch.setattr(download, "_publish_import_intent", retain)

    if failure_kind != "unexpected":
        class Process:
            pid = 4242

            def __init__(self, *_args, **_kwargs):
                self.returncode = 1 if failure_kind == "nonzero" else None
                self.stdout = StringIO("")
                self.stderr = StringIO("simulated provider failure")

            def poll(self):
                return self.returncode

            def wait(self, *_args, **_kwargs):
                return self.returncode

        class Control:
            command = None

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def detach_process(self, *_args):
                return True

        class Heartbeat(Control):
            def transfer_to_pid(self, *_args):
                return True

        async def no_proxy():
            return {"enabled": False}

        def stop_process(proc, **_kwargs):
            proc.returncode = -15
            return proc.returncode

        monkeypatch.setattr(download.subprocess, "Popen", Process)
        monkeypatch.setattr(download, "ControlListener", Control)
        monkeypatch.setattr(download, "HeartbeatPublisher", Heartbeat)
        monkeypatch.setattr(download, "_process_group_exists", lambda *_a: False)
        monkeypatch.setattr(download, "_stop_gallerydl_process", stop_process)
        monkeypatch.setattr(proxy, "_load_proxy_config", no_proxy)

    runner = download.run_download_job
    while hasattr(runner, "__wrapped__"):
        runner = runner.__wrapped__
    await runner(str(job_id))

    await db.rollback()
    job = await db.get(DownloadJob, job_id, populate_existing=True)
    task = await db.get(TaskRun, task_id, populate_existing=True)
    imports = list((await db.execute(
        select(ImportJob).where(ImportJob.download_job_id == job_id)
    )).scalars())
    await db.refresh(artifact)
    await db.refresh(media_artifact)
    dispatch = task.meta[download_dispatch.DISPATCH_META_KEY]
    assert job.status == task.status == "enqueued"
    assert job.retry_count == 1
    assert task.attempts == 2
    assert dispatch["state"] == "pending"
    assert dispatch["action"] == (
        "unexpected_error_retry" if failure_kind == "unexpected" else "auto_retry"
    )
    assert dispatch["delay_seconds"] == 60
    assert imports == []
    assert artifact.import_job_id is None and artifact.state == "new"
    assert media_artifact.import_job_id is None and media_artifact.state == "new"
    assert (
        job.owner_user_id,
        job.triggering_user_subscription_id,
        job.subscription_source_id,
        item.batch_id,
    ) == original_identity
    assert (await db.execute(select(RepositorySyncReceipt).where(
        RepositorySyncReceipt.source_download_job_id == job_id
    ))).scalar_one_or_none() is None

    published_ids = []

    def publish(*_args, **kwargs):
        published_ids.append(kwargs["rq_job_id"])
        return object()

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", publish)
    task.updated_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    await db.commit()
    recovered = await download_dispatch.recover_download_dispatch_outbox(
        db,
        limit=1,
        grace_seconds=1,
    )
    await db.refresh(task)
    assert recovered["replayed"] == 1
    assert published_ids == [dispatch["rq_job_id"]]
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["state"] == "published"


@pytest.mark.asyncio
async def test_programming_fault_terminalizes_exact_pending_dispatch_attempt(
    db,
    monkeypatch,
):
    from app.models import DownloadJob, TaskRun
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services import backpressure, download_dispatch, scheduler_batches

    async def no_pressure(*_args, **_kwargs):
        return None

    async def retain(*_args, **_kwargs):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(scheduler_batches, "_publish_bound_child", retain)
    await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    job = await db.get(DownloadJob, item.download_job_id)
    task = await db.get(TaskRun, item.child_task_id)
    item_id = item.id
    original_retry_count = job.retry_count

    monkeypatch.setattr(
        download_dispatch,
        "_fetch_download_rq_job",
        lambda *_a, **_k: (_ for _ in ()).throw(TypeError("broken recovery call")),
    )
    outcome = await download_dispatch.recover_download_dispatch_candidate(
        db,
        task,
        job,
    )

    await db.refresh(job)
    await db.refresh(task)
    assert outcome == "error"
    assert job.status == task.status == "failed"
    assert job.retry_count == original_retry_count
    assert task.reason_code == "dispatch_recovery_error"
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["state"] == "failed"
    assert task.result_data["download_dispatch_failure"]["error_type"] == "TypeError"
    item = await db.get(SchedulerBatchItem, item_id)
    await scheduler_batches.reconcile_item(db, item)
    assert item.status == "failed"
    assert item.reason_code == "child_failed"
