"""Durable scheduler batches: real PostgreSQL and real enqueue/dispatch paths."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def db():
    from app.database import async_session, engine

    async with async_session() as session:
        await session.execute(text("TRUNCATE task_runs, creators, users RESTART IDENTITY CASCADE"))
        # Durable identities intentionally have no TaskRun/domain cascade.
        from sqlalchemy import inspect

        tables = await session.run_sync(lambda s: inspect(s.bind).get_table_names())
        if "scheduler_batches" in tables:
            await session.execute(text("TRUNCATE scheduler_batch_items, scheduler_batches"))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


async def seed(db, *, manual=False, number=1):
    from app.models import Creator, Subscription, SubscriptionSource, User
    from app.models.remote_discovery import UserSubscription, UserSubscriptionSource

    user = User(username=f"batch-{uuid4()}", password_hash="test-only", is_active=True)
    db.add(user)
    creator = Creator(name=f"batch-{uuid4()}")
    db.add(creator)
    await db.flush()
    sub = Subscription(creator_id=creator.id, name="batch", is_active=True, sync_enabled=not manual, schedule_mode="manual" if manual else "interval")
    db.add(sub)
    await db.flush()
    member = UserSubscription(
        user_id=user.id, subscription_id=sub.id, is_active=True, sync_enabled=not manual, schedule_mode="manual" if manual else "interval"
    )
    db.add(member)
    await db.flush()
    ids = []
    for n in range(number):
        source = SubscriptionSource(
            subscription_id=sub.id,
            source="pixiv",
            source_url=f"https://www.pixiv.net/users/{1000 + n}",
            source_creator_id=str(1000 + n),
            is_enabled=True,
            auth_healthy=True,
        )
        db.add(source)
        await db.flush()
        db.add(
            UserSubscriptionSource(
                user_id=user.id, subscription_id=sub.id, user_subscription_id=member.id, subscription_source_id=source.id, is_enabled=True, auth_healthy=True
            )
        )
        ids.append(source.id)
    await db.commit()
    return ids


async def admit(db, mode="force_eligible", request_id=None):
    from app.api.admin.scheduler import SchedulerSyncNowRequest, trigger_sync_now

    return await trigger_sync_now(SchedulerSyncNowRequest(mode=mode, request_id=request_id), db)


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


async def cleanup_cancelled(db, task_id):
    from app.models import TaskRun
    from app.jobs.admin_operations import _run_registered_admin_operation

    await db.rollback()
    parent = await db.get(TaskRun, UUID(str(task_id)))
    cleanup_id = parent.meta["batch_cancel_cleanup_task_id"]
    cleanup = await db.get(TaskRun, UUID(cleanup_id))
    attempt = cleanup.attempts
    await db.rollback()
    return await _run_registered_admin_operation(cleanup_id, attempt)


async def test_admission_is_durable_bounded_and_idempotent(db):
    from app.models import TaskRun

    await seed(db, number=30)
    request = uuid4()
    accepted = await admit(db, request_id=request)
    assert accepted["status"] == "enqueued"
    assert accepted["operation_type"] == "subscription-sync-batch"
    from app.models.scheduler_batch import SchedulerBatch, SchedulerBatchItem

    batch = (await db.execute(select(SchedulerBatch))).scalar_one()
    assert batch.initialized_at is None
    assert not (await db.execute(select(SchedulerBatchItem))).scalars().all()
    duplicate = await admit(db, request_id=request)
    assert duplicate["task_id"] == accepted["task_id"]
    with pytest.raises(HTTPException) as conflict:
        await admit(db, mode="manual_all_enabled", request_id=request)
    assert conflict.value.status_code == 409
    await db.rollback()
    with pytest.raises(HTTPException) as active:
        await admit(db)
    assert active.value.detail["task_id"] == accepted["task_id"]
    task = await db.get(TaskRun, UUID(accepted["task_id"]))
    assert task.queue_name == "operations"


async def test_zero_candidates_is_explicit_noop(db):
    accepted = await admit(db)
    result = await slice_batch(db, accepted["task_id"])
    assert result["status"] == "noop"
    assert result["candidate_count"] == 0


@pytest.mark.parametrize("mode,expected", [("force_eligible", 0), ("due_scan", 0), ("manual_all_enabled", 1)])
async def test_manual_membership_modes_use_real_enqueue(db, monkeypatch, mode, expected):
    from app.services import backpressure

    async def no_pressure(*args, **kwargs):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    ids = await seed(db, manual=True)
    accepted = await admit(db, mode)
    result = await slice_batch(db, accepted["task_id"])
    from app.models.scheduler_batch import SchedulerBatchItem

    items = (await db.execute(select(SchedulerBatchItem))).scalars().all()
    assert len(items) == expected
    if expected:
        assert items[0].download_job_id is not None
        assert items[0].source_id == ids[0]
        assert result.get("_admin_handoff") is True


async def test_saturation_is_waiting_and_slice_is_bounded(db, monkeypatch):
    from app.services import backpressure

    async def saturated(*args, **kwargs):
        return {"code": "queue_saturated"}

    monkeypatch.setattr(backpressure, "download_backpressure_reason", saturated)
    await seed(db, number=30)
    accepted = await admit(db)
    result = await slice_batch(db, accepted["task_id"])
    from app.models.scheduler_batch import SchedulerBatchItem

    items = (await db.execute(select(SchedulerBatchItem))).scalars().all()
    assert len(items) == 30
    assert sum(i.attempts for i in items) == 25
    assert sum(i.status == "waiting" for i in items) == 25
    assert result["_admin_handoff"] is True
    assert result["failed_count"] == 0


async def test_receipt_survives_compaction_and_import_backlog_blocks_success(db):
    from app.models import DownloadJob, ImportJob, RepositorySyncReceipt, StorageArtifact
    from app.services.scheduler_batches import reconcile_item
    from app.models.scheduler_batch import SchedulerBatchItem

    ids = await seed(db)
    accepted = await admit(db)
    from app.services.scheduler_batches import initialize_batch

    await initialize_batch(db, UUID(accepted["task_id"]))
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    source = await db.get(__import__("app.models", fromlist=["SubscriptionSource"]).SubscriptionSource, ids[0])
    job = DownloadJob(subscription_id=source.subscription_id, subscription_source_id=source.id, source="pixiv", source_url=source.source_url, status="complete")
    db.add(job)
    await db.flush()
    item.download_job_id = job.id
    child = ImportJob(download_job_id=job.id, status="running")
    db.add(child)
    await db.flush()
    await reconcile_item(db, item)
    assert item.status == "importing"
    child.status = "complete"
    artifact = StorageArtifact(
        storage_root="downloads",
        file_path="batch.json",
        source="pixiv",
        creator_dir="batch",
        source_work_id="1",
        file_name="batch.json",
        artifact_type="metadata_json",
        download_job_id=job.id,
        state="failed",
    )
    db.add(artifact)
    await db.flush()
    await reconcile_item(db, item)
    assert item.status == "importing"
    artifact.state = "imported"
    await db.flush()
    await reconcile_item(db, item)
    assert item.status == "succeeded"
    assert (await db.execute(select(RepositorySyncReceipt))).scalar_one().source_download_job_id == job.id
    await db.delete(child)
    await db.flush()
    await db.delete(job)
    await db.flush()
    item.status = "queued"
    await reconcile_item(db, item)
    assert item.status == "succeeded"


async def test_item_uniqueness_and_source_deletion_preserve_identity(db):
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services.scheduler_batches import initialize_batch
    from sqlalchemy.exc import IntegrityError

    ids = await seed(db)
    accepted = await admit(db)
    await initialize_batch(db, UUID(accepted["task_id"]))
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    async with db.begin_nested() as savepoint:
        db.add(SchedulerBatchItem(batch_id=item.batch_id, source_id=ids[0]))
        with pytest.raises(IntegrityError):
            await db.flush()
        await savepoint.rollback()
    await db.execute(text("DELETE FROM user_subscription_sources"))
    await db.execute(text("DELETE FROM subscription_sources"))
    await db.flush()
    assert (await db.execute(select(SchedulerBatchItem.source_id))).scalar_one() == ids[0]


async def test_cancel_fences_pending_attempt_and_preserves_unrelated_jobs(db):
    from app.services.scheduler_batches import cancel_batch, initialize_batch
    from app.services.operations import admin_operation_attempt_context, AdminOperationAttemptRejected
    from app.services.scheduler_batches import run_batch_slice
    from app.models import TaskRun

    await seed(db)
    accepted = await admit(db)
    task_id = UUID(accepted["task_id"])
    await initialize_batch(db, task_id)
    await db.commit()
    result = await cancel_batch(db, task_id, operator="test")
    assert result["status"] == "cancelled"
    with admin_operation_attempt_context(task_id, 1):
        with pytest.raises(AdminOperationAttemptRejected):
            await run_batch_slice(str(task_id), {})
    await db.rollback()
    assert (await db.get(TaskRun, task_id)).status == "cancelled"


async def test_transient_retry_is_fair_and_unexpected_failure_is_isolated(db, monkeypatch):
    from app.services import backpressure
    from app.services.scheduler_batches import run_batch_slice
    from app.models.scheduler_batch import SchedulerBatchItem

    calls = 0

    async def saturated(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"code": "queue_saturated"}

    monkeypatch.setattr(backpressure, "download_backpressure_reason", saturated)
    await seed(db, number=30)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    await slice_batch(db, accepted["task_id"])
    await db.rollback()
    items = (await db.execute(select(SchedulerBatchItem))).scalars().all()
    assert sum(i.attempts for i in items) == 30
    assert calls == 30
    # A broken provider implementation must not fail the whole administrator operation.
    from app.services import subscription_enqueue

    async def explode(*args, **kwargs):
        raise ValueError("bad provider implementation")

    monkeypatch.setattr(subscription_enqueue, "enqueue_subscription_source_sync", explode)
    for item in items:
        item.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    result = await slice_batch(db, accepted["task_id"])
    assert result["failed_count"] == 25
    assert result["_admin_handoff"] is True


async def test_committed_binding_is_visible_before_first_publication(db, monkeypatch):
    from app.services import backpressure, download_dispatch
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.database import async_session

    async def no_pressure(*args, **kwargs):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    observations = []
    real_recover = download_dispatch.recover_download_dispatch_candidate

    async def observe(session, task, job, **kwargs):
        async with async_session() as other:
            bound = (await other.execute(select(SchedulerBatchItem).where(SchedulerBatchItem.download_job_id == job.id))).scalar_one()
            observations.append((bound.source_id, task.parent_task_id))
        return await real_recover(session, task, job, **kwargs)

    monkeypatch.setattr(download_dispatch, "recover_download_dispatch_candidate", observe)
    ids = await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    assert observations == [(ids[0], UUID(accepted["task_id"]))]


async def test_legacy_recovery_is_dry_run_by_default_and_idempotent(db):
    from app.models import TaskRun, RepositorySyncReceipt
    from app.services.scheduler_batches import recover_legacy_batch
    from app.models.scheduler_batch import SchedulerBatchItem

    ids = await seed(db, number=2)
    legacy_id = UUID("3330dcea-e850-422f-9de8-3e5fd62fe6ef")
    original_download = uuid4()
    legacy = TaskRun(
        id=legacy_id,
        kind="admin",
        operation_type="subscription-sync-batch",
        status="complete",
        meta={"mode": "manual_all_enabled"},
        result_data={
            "mode": "manual_all_enabled",
            "candidate_count": 2,
            "job_ids": [str(original_download)],
            "skipped": [{"source_id": str(ids[1]), "skip_reason": "queue_saturated"}],
        },
    )
    db.add(legacy)
    db.add(
        RepositorySyncReceipt(
            repository_id=ids[0], source_download_job_id=original_download, source="pixiv", status="complete", finished_at=datetime.now(timezone.utc)
        )
    )
    await db.commit()
    preview = await recover_legacy_batch(db)
    assert preview["dry_run"] is True
    assert preview["candidate_count"] == 2
    applied = await recover_legacy_batch(db, apply=True)
    replay = await recover_legacy_batch(db, apply=True)
    assert applied["task_id"] == replay["task_id"]
    items = (await db.execute(select(SchedulerBatchItem))).scalars().all()
    assert len(items) == 2
    assert next(i for i in items if i.source_id == ids[0]).status == "succeeded"
    assert next(i for i in items if i.source_id == ids[1]).download_job_id is None
    assert (await db.get(TaskRun, legacy_id)).result_data["candidate_count"] == 2


async def test_http_permissions_and_system_only_batch_control(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api.admin import router as admin_router
    from app.api.tasks import router as task_router
    from app.auth import create_access_token
    from app.models import User, TaskRun

    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    app.include_router(task_router, prefix="/tasks")
    users = {}
    for role in ["system", "tasks"]:
        user = User(username=f"batch-{role}", password_hash="test", permissions=[role], is_active=True)
        db.add(user)
        users[role] = user
    await db.commit()
    headers = {role: {"Authorization": f"Bearer {create_access_token(user.username)}"} for role, user in users.items()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/admin/scheduler/sync-now")).status_code == 401
        assert (await client.post("/admin/scheduler/sync-now", headers=headers["tasks"])).status_code == 403
        response = await client.post("/admin/scheduler/sync-now", headers=headers["system"])
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        assert (await client.get(f"/tasks/{task_id}", headers=headers["tasks"])).status_code == 403
        assert (await client.get(f"/tasks/{task_id}", headers=headers["system"])).status_code == 200
        unrelated = TaskRun(kind="download", status="enqueued", owner_user_id=users["system"].id)
        db.add(unrelated)
        await db.commit()
        assert (await client.get(f"/tasks/{unrelated.id}", headers=headers["system"])).status_code == 403
        assert (await client.post(f"/tasks/{task_id}/cancel", headers=headers["system"])).status_code == 200


async def test_legacy_missing_identity_refuses_apply(db):
    from app.models import TaskRun
    from app.services.scheduler_batches import recover_legacy_batch, LEGACY_ID

    db.add(
        TaskRun(
            id=LEGACY_ID,
            kind="admin",
            operation_type="subscription-sync-batch",
            status="complete",
            result_data={"candidate_count": 1, "job_ids": [str(uuid4())]},
        )
    )
    await db.commit()
    assert len((await recover_legacy_batch(db))["unresolved_download_ids"]) == 1
    with pytest.raises(HTTPException) as exc:
        await recover_legacy_batch(db, apply=True)
    assert exc.value.detail["code"] == "legacy_identity_unresolved"


async def test_restart_replays_committed_identity_without_duplicate_download(db, monkeypatch):
    from app.services import scheduler_batches, backpressure
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.models import DownloadJob

    async def no_pressure(*args, **kwargs):
        return None

    async def crash_before_publish(*args):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    real_publish = scheduler_batches._publish_bound_child
    monkeypatch.setattr(scheduler_batches, "_publish_bound_child", crash_before_publish)
    await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    identity = item.download_job_id
    await real_publish(item.id)
    await real_publish(item.id)
    assert (await db.execute(select(DownloadJob.id))).scalars().all() == [identity]


async def test_fast_worker_compaction_during_first_publication_uses_receipt(db, monkeypatch):
    import asyncio
    from app.database import async_session
    from app.services import backpressure, download_dispatch
    from app.models import DownloadJob, RepositorySyncReceipt, TaskRun
    from app.models.scheduler_batch import SchedulerBatchItem

    async def no_pressure(*args, **kwargs):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    loop = asyncio.get_running_loop()

    async def compact(job_id):
        from sqlalchemy import delete

        async with async_session() as other:
            job = await other.get(DownloadJob, UUID(job_id))
            other.add(
                RepositorySyncReceipt(
                    repository_id=job.subscription_source_id,
                    source_download_job_id=job.id,
                    source=job.source,
                    status="complete",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            await other.execute(delete(TaskRun).where(TaskRun.subject_id == job.id))
            await other.delete(job)
            await other.commit()

    def fast_worker(queue, function, job_id, **kwargs):
        asyncio.run_coroutine_threadsafe(compact(job_id), loop).result(timeout=10)
        return object()

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", fast_worker)
    await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.status != "failed"
    from app.services.scheduler_batches import reconcile_item

    await reconcile_item(db, item)
    assert item.status == "succeeded"


async def test_recovery_preparation_failure_does_not_publish_uninitialized_batch(db, monkeypatch):
    from app.models import TaskRun, RepositorySyncReceipt
    from app.models.scheduler_batch import SchedulerBatch
    from app.services import scheduler_batches
    from app.database import async_session

    ids = await seed(db)
    job_id = uuid4()
    db.add(
        TaskRun(
            id=scheduler_batches.LEGACY_ID,
            kind="admin",
            operation_type="subscription-sync-batch",
            status="complete",
            result_data={"candidate_count": 1, "job_ids": [str(job_id)]},
        )
    )
    db.add(
        RepositorySyncReceipt(repository_id=ids[0], source_download_job_id=job_id, source="pixiv", status="complete", finished_at=datetime.now(timezone.utc))
    )
    await db.commit()

    async def crash(*args):
        raise RuntimeError("process terminated during recovery preparation")

    monkeypatch.setattr(scheduler_batches, "reconcile_item", crash)
    with pytest.raises(RuntimeError):
        await scheduler_batches.recover_legacy_batch(db, apply=True)
    async with async_session() as other:
        assert not (await other.execute(select(SchedulerBatch))).scalars().all()


async def test_ready_successor_is_published_after_registered_slice_without_recovery_tick(db, monkeypatch):
    from app.services import backpressure, operations
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.services.redis_client import get_redis

    async def saturated(*args, **kwargs):
        return {"code": "queue_saturated"}

    monkeypatch.setattr(backpressure, "download_backpressure_reason", saturated)
    await seed(db, number=30)
    accepted = await admit(db)
    result = await _run_registered_admin_operation(accepted["task_id"], 1)
    assert result["_admin_handoff"] is True
    rq_id = operations.deterministic_admin_rq_job_id(accepted["task_id"], 2)
    assert get_redis().exists(f"rq:job:{rq_id}")


async def test_cancel_vs_snapshot_uses_parent_only_fence_without_deadlock(db, monkeypatch):
    import asyncio
    from app.database import async_session
    from app.services import scheduler_batches
    from app.services.operations import admin_operation_attempt_context, AdminOperationAttemptRejected

    accepted = await admit(db)
    task_id = UUID(accepted["task_id"])
    snapshot_locked = asyncio.Event()
    release_snapshot = asyncio.Event()
    real_fence = scheduler_batches.fence_current_admin_operation_transaction

    async def gated_fence(session, **kwargs):
        snapshot_locked.set()
        await release_snapshot.wait()
        return await real_fence(session, **kwargs)

    monkeypatch.setattr(scheduler_batches, "fence_current_admin_operation_transaction", gated_fence)

    async def initialize():
        async with async_session() as session:
            with admin_operation_attempt_context(task_id, 1):
                try:
                    await scheduler_batches.initialize_batch(session, task_id)
                    await session.commit()
                except AdminOperationAttemptRejected:
                    await session.rollback()

    async def cancel():
        async with async_session() as session:
            return await scheduler_batches.cancel_batch(session, task_id, operator="race-test")

    initializing = asyncio.create_task(initialize())
    await snapshot_locked.wait()
    cancelling = asyncio.create_task(cancel())
    await asyncio.sleep(0.15)
    release_snapshot.set()
    await asyncio.wait_for(asyncio.gather(initializing, cancelling), timeout=10)


async def test_cancel_serializes_with_first_publication_and_blocks_replay(db, monkeypatch):
    import asyncio
    import threading
    from app.database import async_session
    from app.services import scheduler_batches, backpressure, download_dispatch
    from app.models import DownloadJob, TaskRun
    from app.models.scheduler_batch import SchedulerBatchItem

    async def no_pressure(*args, **kwargs):
        return None

    async def delay_publication(*args):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    real_publish = scheduler_batches._publish_bound_child
    monkeypatch.setattr(scheduler_batches, "_publish_bound_child", delay_publication)
    await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    item_id, job_id, child_id = item.id, item.download_job_id, item.child_task_id
    entered = threading.Event()
    release = threading.Event()
    real_enqueue = download_dispatch.enqueue_download_rq

    def blocked_enqueue(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", blocked_enqueue)
    publishing = asyncio.create_task(real_publish(item_id))
    assert await asyncio.to_thread(entered.wait, 10)

    async def cancel():
        async with async_session() as session:
            return await scheduler_batches.cancel_batch(session, UUID(accepted["task_id"]), operator="race-test")

    cancelling = asyncio.create_task(cancel())
    await asyncio.sleep(0.1)
    assert not cancelling.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(publishing, cancelling), timeout=15)
    await cleanup_cancelled(db, accepted["task_id"])
    await db.rollback()
    job = await db.get(DownloadJob, job_id)
    child = await db.get(TaskRun, child_id)
    assert job.status == "cancelled"
    assert await download_dispatch.recover_download_dispatch_candidate(db, child, job) == "skipped"


async def test_publication_pressure_keeps_bound_identity_waiting_then_resumes(db, monkeypatch):
    from app.services import backpressure, download_dispatch, scheduler_batches
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.models import DownloadJob

    async def no_pressure(*args, **kwargs):
        return None

    real_enqueue = download_dispatch.enqueue_download_rq

    def saturated(*args, **kwargs):
        raise backpressure.DownloadAdmissionError("queue_saturated", "full")

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", saturated)
    await seed(db)
    accepted = await admit(db)
    await slice_batch(db, accepted["task_id"])
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.status == "waiting"
    assert item.download_job_id is not None
    identity = item.download_job_id
    item.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", real_enqueue)
    await slice_batch(db, accepted["task_id"])
    await db.rollback()
    assert (await db.execute(select(DownloadJob.id))).scalars().all() == [identity]
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.status == "queued"


async def test_batch_parent_is_not_compacted_away_from_request_identity(db):
    from app.models import TaskRun
    from app.models.scheduler_batch import SchedulerBatch
    from app.services.operation_attention import compact_terminal_tasks

    accepted = await admit(db)
    old = await db.get(TaskRun, UUID(accepted["task_id"]))
    old.status = "complete"
    old.finished_at = datetime.now(timezone.utc) - timedelta(days=3)
    old.compactable_at = old.finished_at
    batch = (await db.execute(select(SchedulerBatch))).scalar_one()
    batch.state = "complete"
    await db.commit()
    newer = await admit(db)
    latest = await db.get(TaskRun, UUID(newer["task_id"]))
    latest.status = "complete"
    latest.finished_at = datetime.now(timezone.utc)
    await db.commit()
    await compact_terminal_tasks(db, dry_run=False, limit=100)
    await db.commit()
    await db.rollback()
    assert await db.get(TaskRun, UUID(accepted["task_id"])) is not None


async def test_additive_migration_matches_real_postgres_constraints(db):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic/versions/fa02b4c6d8e0_durable_scheduler_batches.py"
    spec = importlib.util.spec_from_file_location("scheduler_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    await db.execute(text("DROP TABLE scheduler_batch_items"))
    await db.execute(text("DROP TABLE scheduler_batches"))

    def upgrade(session):
        migration.op = Operations(MigrationContext.configure(session.connection()))
        migration.upgrade()

    await db.run_sync(upgrade)
    await db.commit()
    accepted = await admit(db)
    assert accepted["status"] == "enqueued"

    def downgrade(session):
        migration.op = Operations(MigrationContext.configure(session.connection()))
        migration.downgrade()

    with pytest.raises(Exception, match="Drain scheduler batches"):
        await db.run_sync(downgrade)
    await db.rollback()


@pytest.mark.parametrize("failure", [False, True])
async def test_registered_terminal_outcome_keeps_aggregate_progress(db, monkeypatch, failure):
    from app.models import TaskRun, SubscriptionSource
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.services import subscription_enqueue

    ids = await seed(db)
    source = await db.get(SubscriptionSource, ids[0])
    source.source = "not-a-provider"
    await db.commit()
    if failure:

        async def broken(*args, **kwargs):
            raise ValueError("provider bug")

        monkeypatch.setattr(subscription_enqueue, "enqueue_subscription_source_sync", broken)
    accepted = await admit(db)
    result = await _run_registered_admin_operation(accepted["task_id"], 1)
    task = await db.get(TaskRun, UUID(accepted["task_id"]), populate_existing=True)
    assert task.status == ("failed" if failure else "complete")
    assert result["status"] == ("partial_error" if failure else "noop")
    assert task.progress_data["total"] == 1
    assert task.progress_data["current"] == 1
    assert task.progress_data["failed_count" if failure else "skipped_count"] == 1


async def test_manual_batch_uses_enabled_member_policy_after_real_cache_recompute(db, monkeypatch):
    from app.models import SubscriptionSource
    from app.models.remote_discovery import UserSubscriptionSource
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services.subscription_membership import recompute_subscription_membership_cache
    from app.services import backpressure

    async def no_pressure(*args, **kwargs):
        return None

    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    ids = await seed(db, manual=True, number=2)
    source = await db.get(SubscriptionSource, ids[0])
    disabled = (await db.execute(select(UserSubscriptionSource).where(UserSubscriptionSource.subscription_source_id == ids[1]))).scalar_one()
    disabled.is_enabled = False
    await recompute_subscription_membership_cache(db, source.subscription_id)
    await db.commit()
    assert source.is_enabled is False  # Automatic cache legitimately excludes manual-only demand.
    accepted = await admit(db, "manual_all_enabled")
    await slice_batch(db, accepted["task_id"])
    items = (await db.execute(select(SchedulerBatchItem))).scalars().all()
    assert len(items) == 1
    assert items[0].source_id == ids[0]
    assert items[0].download_job_id is not None


@pytest.mark.parametrize("child_status", ["failed", "stale", "cancelled"])
async def test_terminal_import_failure_is_not_hidden_by_failed_artifacts(db, child_status):
    from app.models import DownloadJob, ImportJob, StorageArtifact, SubscriptionSource
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services.scheduler_batches import initialize_batch, reconcile_item

    ids = await seed(db)
    accepted = await admit(db)
    await initialize_batch(db, UUID(accepted["task_id"]))
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    source = await db.get(SubscriptionSource, ids[0])
    job = DownloadJob(
        subscription_id=source.subscription_id, subscription_source_id=source.id, source="pixiv", source_url=source.source_url, status="importing"
    )
    db.add(job)
    await db.flush()
    db.add(ImportJob(download_job_id=job.id, status=child_status, import_retry_count=3))
    db.add(
        StorageArtifact(
            storage_root="downloads",
            file_path="failed.json",
            source="pixiv",
            creator_dir="failure",
            source_work_id="1",
            file_name="failed.json",
            artifact_type="metadata_json",
            download_job_id=job.id,
            state="failed",
        )
    )
    item.download_job_id = job.id
    await db.flush()
    await reconcile_item(db, item)
    assert item.status == "failed"
    assert item.reason_code == f"child_{child_status}"


async def test_openapi_has_typed_acceptance_and_item_page():
    from fastapi import FastAPI
    from app.api.admin import router

    app = FastAPI()
    app.include_router(router, prefix="/admin")
    schema = app.openapi()
    receipt = schema["paths"]["/admin/scheduler/sync-now"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]
    assert "$ref" in receipt
    definition = schema["components"]["schemas"][receipt["$ref"].rsplit("/", 1)[1]]
    assert {"task_id", "job_id", "status", "operation_type", "mode"} <= definition["properties"].keys()
    page = schema["paths"]["/admin/scheduler/batches/{task_id}/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in page


async def test_cancel_preserves_unrelated_running_child_and_persists_final_counts(db):
    from app.models import DownloadJob, SubscriptionSource, TaskRun
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services.scheduler_batches import initialize_batch, cancel_batch

    ids = await seed(db)
    source = await db.get(SubscriptionSource, ids[0])
    job = DownloadJob(
        subscription_id=source.subscription_id, subscription_source_id=source.id, source="pixiv", source_url=source.source_url, status="downloading"
    )
    db.add(job)
    await db.commit()
    job_id = job.id
    accepted = await admit(db)
    task_id = UUID(accepted["task_id"])
    await initialize_batch(db, task_id)
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    item.download_job_id = job.id
    item.owns_download = False
    await db.commit()
    await cancel_batch(db, task_id, operator="test")
    await cleanup_cancelled(db, task_id)
    await db.rollback()
    assert (await db.get(DownloadJob, job_id)).status == "downloading"
    task = await db.get(TaskRun, task_id)
    assert task.result_data["cancelled_count"] == 1
    assert task.result_data["status"] == "cancelled"
    assert (await cancel_batch(db, task_id, operator="test"))["cleanup_pending"] is False


async def test_cancel_admission_defers_large_pending_cleanup_to_durable_operation(db):
    from app.models import TaskRun
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services.scheduler_batches import initialize_batch, cancel_batch

    await seed(db, number=33)
    accepted = await admit(db)
    task_id = UUID(accepted["task_id"])
    await initialize_batch(db, task_id)
    await db.commit()
    cancelled = await cancel_batch(db, task_id, operator="test")
    assert cancelled["cleanup_task_id"]
    assert (await db.get(TaskRun, task_id, populate_existing=True)).status == "cancelled"
    cleanup = await db.get(TaskRun, UUID(cancelled["cleanup_task_id"]))
    assert cleanup.status == "enqueued"
    assert cleanup.queue_name == "operations"
    items = (await db.execute(select(SchedulerBatchItem))).scalars().all()
    assert len(items) == 33
    assert all(item.status == "pending" for item in items)
    # Cleanup has a distinct scope; a new batch can be admitted immediately.
    assert (await admit(db))["task_id"] != accepted["task_id"]
    await cleanup_cancelled(db, task_id)
    await db.rollback()
    assert (await db.get(TaskRun, task_id)).result_data["cancelled_count"] == 33


async def test_cancel_http_never_waits_for_child_redis_control(db, monkeypatch):
    from app.models import DownloadJob, SubscriptionSource
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services.scheduler_batches import initialize_batch, cancel_batch
    from app.services.redis_pubsub import TaskEventPublisher
    from redis.exceptions import ConnectionError

    ids = await seed(db)
    source = await db.get(SubscriptionSource, ids[0])
    job = DownloadJob(
        subscription_id=source.subscription_id, subscription_source_id=source.id, source="pixiv", source_url=source.source_url, status="downloading"
    )
    db.add(job)
    await db.commit()
    accepted = await admit(db)
    task_id = UUID(accepted["task_id"])
    await initialize_batch(db, task_id)
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    item.download_job_id = job.id
    item.owns_download = True
    await db.commit()

    def offline(*args, **kwargs):
        raise ConnectionError("isolated test Redis outage")

    real_send = TaskEventPublisher.send_control
    monkeypatch.setattr(TaskEventPublisher, "send_control", offline)
    response = await cancel_batch(db, task_id, operator="test")
    assert response["cleanup_task_id"]

    result = await cleanup_cancelled(db, task_id)
    assert result["cleanup_pending"] is True
    await db.rollback()
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.reason_code == "cancel_retry"
    item.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()
    monkeypatch.setattr(TaskEventPublisher, "send_control", real_send)
    result = await cleanup_cancelled(db, task_id)
    assert result["cleanup_pending"] is False


async def test_transient_snapshot_failure_yields_durable_retry(db, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from app.services import scheduler_batches
    from app.models import TaskRun
    accepted = await admit(db)
    real_initialize = scheduler_batches.initialize_batch
    async def unavailable(*args, **kwargs):
        raise OperationalError('snapshot', {}, OSError('temporary database disconnect'))
    monkeypatch.setattr(scheduler_batches, 'initialize_batch', unavailable)
    result = await slice_batch(db, accepted['task_id'])
    assert result['_admin_handoff'] is True
    task = await db.get(TaskRun, UUID(accepted['task_id']), populate_existing=True)
    assert task.status == 'enqueued'
    assert task.attempts == 2
    monkeypatch.setattr(scheduler_batches, 'initialize_batch', real_initialize)
    assert (await slice_batch(db, accepted['task_id']))['status'] == 'noop'


async def test_due_scan_respects_disabled_scheduler_configuration(db, monkeypatch):
    from app.services import subscription_enqueue, backpressure
    from app.models.scheduler_batch import SchedulerBatchItem
    async def disabled_config(*args, **kwargs):
        return {'scheduler_enabled': False, 'schedule_mode': 'interval'}
    async def no_pressure(*args, **kwargs):
        return None
    monkeypatch.setattr(subscription_enqueue, 'get_scheduler_config', disabled_config)
    monkeypatch.setattr(backpressure, 'download_backpressure_reason', no_pressure)
    await seed(db)
    accepted = await admit(db, 'due_scan')
    result = await slice_batch(db, accepted['task_id'])
    assert result['status'] == 'noop'
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.reason_code == 'scheduler_disabled'
    assert item.download_job_id is None


async def test_batch_redis_budget_bounds_blackhole_and_preserves_global_timeouts():
    import asyncio
    import time
    from redis.exceptions import RedisError
    from app.services.redis_budget import budget_redis
    from app.services.redis_client import get_redis
    ordinary = get_redis()
    original_timeout = ordinary.connection_pool.connection_kwargs['socket_timeout']
    release = asyncio.Event()
    connections = []
    async def blackhole(reader, writer):
        connections.append(writer)
        await release.wait()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(blackhole, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    started = time.monotonic()
    try:
        with budget_redis(seconds=2, reserve_seconds=.25, url=f'redis://127.0.0.1:{port}/0'):
            bounded = get_redis()
            assert bounded is not ordinary
            assert bounded.connection_pool.connection_kwargs['socket_timeout'] <= .5
            with pytest.raises(RedisError):
                await asyncio.to_thread(bounded.ping)
        elapsed = time.monotonic() - started
        print(f'Blackhole Redis bounded ping: {elapsed:.3f}s')
        assert elapsed < 1.5
        assert get_redis() is ordinary
        assert ordinary.connection_pool.connection_kwargs['socket_timeout'] == original_timeout
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(.05)


async def test_full_batch_slice_defers_blackholed_redis_within_its_budget(db):
    import asyncio
    import time
    from app.services.redis_budget import budget_redis
    from app.models.scheduler_batch import SchedulerBatchItem
    release = asyncio.Event()
    async def blackhole(reader, writer):
        await release.wait()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(blackhole, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    await seed(db)
    accepted = await admit(db)
    started = time.monotonic()
    try:
        with budget_redis(seconds=3, reserve_seconds=.5, url=f'redis://127.0.0.1:{port}/0'):
            result = await slice_batch(db, accepted['task_id'])
        elapsed = time.monotonic() - started
        print(f'Full batch slice with blackholed Redis: {elapsed:.3f}s (3s test budget)')
        assert elapsed < 3.5
        assert result['_admin_handoff'] is True
        item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
        assert item.status == 'waiting'
        assert item.download_job_id is None
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(.05)


async def test_locked_eligible_membership_defers_then_admits_once(db, monkeypatch):
    import asyncio
    from app.database import async_session
    from app.models import DownloadJob
    from app.models.remote_discovery import UserSubscriptionSource
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services import backpressure

    async def no_pressure(*args, **kwargs):
        return None
    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    source_id = (await seed(db))[0]
    accepted = await admit(db)
    async with async_session() as locker:
        await locker.execute(select(UserSubscriptionSource).where(
            UserSubscriptionSource.subscription_source_id == source_id).with_for_update())
        await asyncio.wait_for(slice_batch(db, accepted["task_id"]), timeout=8)
        item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
        assert item.status == "waiting"
        assert item.reason_code == "membership_lock_busy"
        assert 0 < (item.next_retry_at - datetime.now(timezone.utc)).total_seconds() <= 30
        assert not (await db.execute(select(DownloadJob))).scalars().all()
        item.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await locker.rollback()
    await slice_batch(db, accepted["task_id"])
    await db.rollback()
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    jobs = (await db.execute(select(DownloadJob))).scalars().all()
    assert len(jobs) == 1
    assert item.download_job_id == jobs[0].id
    assert item.owns_download is True


@pytest.mark.parametrize("with_import", [False, True])
async def test_cancel_preserves_completion_between_reconcile_and_domain_lock(db, monkeypatch, with_import):
    from app.database import async_session
    from app.models import DownloadJob, ImportJob, SubscriptionSource, TaskRun
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services import scheduler_batches
    from app.services.operation_attention import upsert_repository_sync_receipt

    source_id = (await seed(db))[0]
    source = await db.get(SubscriptionSource, source_id)
    job = DownloadJob(subscription_id=source.subscription_id, subscription_source_id=source_id,
                      source="pixiv", source_url=source.source_url, status="importing" if with_import else "downloading")
    db.add(job)
    await db.flush()
    if with_import:
        db.add(ImportJob(download_job_id=job.id, status="running"))
    await db.commit()
    job_id = job.id
    task_id = UUID((await admit(db))["task_id"])
    await scheduler_batches.initialize_batch(db, task_id)
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    item.download_job_id, item.owns_download = job_id, True
    await db.commit()
    await scheduler_batches.cancel_batch(db, task_id, operator="race-test")
    real_reconcile = scheduler_batches.reconcile_item
    completed = False

    async def complete_after_first_reconcile(session, item):
        nonlocal completed
        await real_reconcile(session, item)
        if not completed:
            completed = True
            async with async_session() as worker:
                current = await worker.get(DownloadJob, job_id)
                if with_import:
                    child = (await worker.execute(select(ImportJob).where(ImportJob.download_job_id == job_id))).scalar_one()
                    child.status = "complete"
                current.status = "complete"
                await worker.flush()
                await worker.refresh(current)
                await upsert_repository_sync_receipt(worker, current, status="complete")
                await worker.commit()
    monkeypatch.setattr(scheduler_batches, "reconcile_item", complete_after_first_reconcile)
    await cleanup_cancelled(db, task_id)
    await db.rollback()
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert completed
    assert item.status == "succeeded"
    assert item.outcome["status"] == "complete"
    assert item.outcome["receipt_id"]
    parent = await db.get(TaskRun, task_id)
    assert parent.result_data["succeeded_count"] == 1
    assert parent.result_data["cancelled_count"] == 0
    assert (await db.get(DownloadJob, job_id)).status == "complete"


@pytest.mark.parametrize("compacted", [False, True])
async def test_legacy_unbound_item_rechecks_completion_after_preparation(db, monkeypatch, compacted):
    from app.models import DownloadJob, SubscriptionSource, TaskRun, RepositorySyncReceipt
    from app.models.scheduler_batch import SchedulerBatchItem
    from app.services import scheduler_batches, backpressure

    async def no_pressure(*args, **kwargs):
        return None
    monkeypatch.setattr(backpressure, "download_backpressure_reason", no_pressure)
    source_id = (await seed(db))[0]
    db.add(TaskRun(id=scheduler_batches.LEGACY_ID, kind="admin", operation_type="subscription-sync-batch",
                   status="complete", created_at=datetime.now(timezone.utc) - timedelta(days=1),
                   result_data={"candidate_count": 1, "mode": "manual_all_enabled", "job_ids": [],
                                "skipped": [{"source_id": str(source_id), "skip_reason": "queue_saturated"}]}))
    await db.commit()
    recovered = await scheduler_batches.recover_legacy_batch(db, apply=True)
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.download_job_id is None
    source = await db.get(SubscriptionSource, source_id)
    later_id = uuid4()
    if not compacted:
        db.add(DownloadJob(id=later_id, subscription_id=source.subscription_id, subscription_source_id=source_id,
                           source="pixiv", source_url=source.source_url, status="complete"))
    db.add(RepositorySyncReceipt(repository_id=source_id, source_download_job_id=later_id,
                                 source="pixiv", status="complete", finished_at=datetime.now(timezone.utc)))
    await db.commit()
    from app.database import async_session
    from sqlalchemy.exc import DBAPIError
    real_lookup = scheduler_batches.subsequent_sync_identity
    lock_checked = False

    async def lookup_with_lock_observation(session, source_id, cutoff):
        nonlocal lock_checked
        async with async_session() as observer:
            with pytest.raises(DBAPIError) as locked:
                await observer.execute(select(SubscriptionSource).where(
                    SubscriptionSource.id == source_id).with_for_update(nowait=True))
            assert locked.value.orig.sqlstate == "55P03"
            await observer.rollback()
        lock_checked = True
        return await real_lookup(session, source_id, cutoff)
    monkeypatch.setattr(scheduler_batches, "subsequent_sync_identity", lookup_with_lock_observation)
    await slice_batch(db, recovered["task_id"])
    await db.rollback()
    assert lock_checked
    item = (await db.execute(select(SchedulerBatchItem))).scalar_one()
    assert item.download_job_id == later_id
    assert item.status == "succeeded"
    assert item.owns_download is False
    jobs = (await db.execute(select(DownloadJob))).scalars().all()
    assert {j.id for j in jobs} == (set() if compacted else {later_id})
