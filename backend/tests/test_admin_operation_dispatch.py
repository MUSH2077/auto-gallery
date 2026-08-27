"""Durable administrator-operation dispatch and lock-order regressions."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import redis as redis_lib
from sqlalchemy import delete, select, text


async def _clear_dispatch_rows(db) -> None:
    from app.models import TaskEvent, TaskRun

    await db.execute(delete(TaskEvent))
    await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
    await db.commit()


def _clear_rq_task(redis_client, *task_ids) -> None:
    keys = []
    for task_id in task_ids:
        for attempt in range(1, 10):
            job_id = f"admin-{task_id}-attempt-{attempt}"
            for queue_name in ("maintenance", "operations", "imports"):
                redis_client.lrem(f"rq:queue:{queue_name}", 0, job_id)
        keys.extend(redis_client.scan_iter(match=f"*{task_id}*"))
    if keys:
        redis_client.delete(*set(keys))


@pytest.mark.asyncio
async def test_file_publication_finalizer_survives_outer_cancellation():
    """Lease-loss cancellation must wait for the fenced final commit to settle."""
    from app.jobs.admin_operations import _await_admin_file_finalizer

    entered = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def finalize():
        nonlocal completed
        entered.set()
        await release.wait()
        completed = True
        return {"status": "complete"}

    waiter = asyncio.create_task(_await_admin_file_finalizer(finalize()))
    await entered.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert completed is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_committed_admin_intent_recovers_without_redis_authority():
    """A crash after commit but before Redis publication remains recoverable."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=7 * 24 * 60 * 60,
            )
            task_id = prepared.task.id
            await db.commit()

        # A new process/session sees only PostgreSQL and republishes the exact
        # committed attempt. No Redis lock or status projection is required.
        recovered = await operations.recover_admin_operation_dispatches(
            redis_client=redis_client,
            grace_seconds=0,
        )
        assert recovered == {"scanned": 1, "published": 1, "deferred": 0, "failed": 0}

        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert task.attempts == 1
            assert task.rq_job_id == f"admin-{task_id}-attempt-1"
            assert dispatch["publication_state"] == "published"
            assert dispatch["options"] == {}
            assert dispatch["scope_key"] == "library:search-reindex:active"
            assert dispatch["attempt"] == 1
    finally:
        if task_id is not None:
            _clear_rq_task(redis_client, task_id)
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lost_enqueue_response_reuses_real_rq_record(monkeypatch):
    """An accepted RQ write with a lost response is acknowledged, not duplicated."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    task_id = None
    original_enqueue = operations._enqueue_admin_rq
    response_lost = True

    def enqueue_then_lose(*args, **kwargs):
        nonlocal response_lost
        job = original_enqueue(*args, **kwargs)
        if response_lost:
            response_lost = False
            raise redis_lib.ConnectionError("response lost after Redis accepted enqueue")
        return job

    monkeypatch.setattr(operations, "_enqueue_admin_rq", enqueue_then_lose)
    try:
        result = await operations.start_admin_operation(
            operation_type="admin-search-reindex",
            scope_key="library:search-reindex:active",
            title="Search reindex",
            entity="search-reindex",
            options={},
            queue_name="maintenance",
            job_timeout=7 * 24 * 60 * 60,
            redis_client=redis_client,
        )
        task_id = UUID(result["task_id"])
        assert result["status"] == "enqueued"

        async with async_session() as pending_db:
            pending = await pending_db.get(TaskRun, task_id)
            assert pending.meta[operations.ADMIN_DISPATCH_META_KEY]["publication_state"] == "pending"

        recovered = await operations.recover_admin_operation_dispatches(
            redis_client=redis_client,
            now=datetime.now(timezone.utc) + timedelta(seconds=31),
            grace_seconds=0,
        )
        assert recovered["published"] == 1
        queued_ids = [
            value.decode() if isinstance(value, bytes) else value
            for value in redis_client.lrange("rq:queue:maintenance", 0, -1)
        ]
        assert queued_ids.count(result["job_id"]) == 1

        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.rq_job_id == result["job_id"]
            assert task.meta[operations.ADMIN_DISPATCH_META_KEY]["publication_state"] == "published"
    finally:
        if task_id is not None:
            _clear_rq_task(redis_client, task_id)
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redis_outage_keeps_api_start_pending_with_bounded_backoff():
    """Transport loss cannot fail a committed administrator TaskRun."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    class UnavailableRedis:
        def __getattr__(self, _name):
            raise redis_lib.ConnectionError("redis unavailable")

    task_id = None
    before = datetime.now(timezone.utc)
    try:
        result = await operations.start_admin_operation(
            operation_type="admin-search-reindex",
            scope_key="library:search-reindex:active",
            title="Search reindex",
            entity="search-reindex",
            options={},
            queue_name="maintenance",
            job_timeout=7 * 24 * 60 * 60,
            redis_client=UnavailableRedis(),
        )
        task_id = UUID(result["task_id"])
        assert result["status"] == "enqueued"

        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            retry_at = datetime.fromisoformat(dispatch["next_retry_at"])
            assert task.status == "enqueued"
            assert dispatch["publication_state"] == "pending"
            assert dispatch["publication_failures"] == 1
            assert before + timedelta(seconds=30) <= retry_at <= before + timedelta(seconds=35)
            assert "redis unavailable" in dispatch["last_error"]
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_queue_capacity_rejection_is_a_recoverable_publication_failure(monkeypatch):
    """A healthy Redis read followed by admission rejection still returns 202 state."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.queue_admission import QueueAdmissionError
    from app.services.redis_client import get_redis

    task_id = None
    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            QueueAdmissionError("redis_capacity", "Redis is at capacity")
        ),
    )
    try:
        result = await operations.start_admin_operation(
            operation_type="admin-search-reindex",
            scope_key="library:search-reindex:active",
            title="Search reindex",
            entity="search-reindex",
            options={},
            queue_name="maintenance",
            job_timeout=60,
            redis_client=get_redis(),
        )
        task_id = UUID(result["task_id"])
        assert result["status"] == "enqueued"
        assert result["publication_state"] == operations.ADMIN_DISPATCH_PENDING
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.meta[operations.ADMIN_DISPATCH_META_KEY]["publication_failures"] == 1
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_prioritizes_later_pending_intent_over_old_published_probes(
    monkeypatch,
):
    """Published probes cannot consume the batch ahead of a due pending intent."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    published_ids: set[str] = set()
    enqueued_ids: list[str] = []
    base = datetime.now(timezone.utc) - timedelta(hours=2)

    class QueuedJob:
        result = None

        @staticmethod
        def get_status(*, refresh=True):
            del refresh
            return "queued"

    def fetch(rq_job_id, *, redis_client=None):
        del redis_client
        return QueuedJob() if rq_job_id in published_ids else None

    def enqueue(*_args, rq_job_id, **_kwargs):
        enqueued_ids.append(rq_job_id)
        return SimpleNamespace(id=rq_job_id)

    monkeypatch.setattr(operations, "_fetch_admin_rq", fetch)
    monkeypatch.setattr(operations, "_enqueue_admin_rq", enqueue)
    pending_id = None
    old_published_task_ids: list[UUID] = []
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            for index in range(26):
                prepared = await operations.prepare_admin_operation(
                    db,
                    operation_type="admin-gitllery-verify",
                    scope_key=f"gitllery:verify:{uuid4()}",
                    title=f"Published probe {index}",
                    entity="gitllery-verify",
                    options={"repository_id": str(uuid4())},
                    queue_name="maintenance",
                    job_timeout=60,
                )
                prepared.task.created_at = base + timedelta(seconds=index)
                dispatch = dict(
                    prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY]
                )
                dispatch["prepared_at"] = base.isoformat()
                dispatch["publication_state"] = operations.ADMIN_DISPATCH_PUBLISHED
                prepared.task.meta = {
                    **prepared.task.meta,
                    operations.ADMIN_DISPATCH_META_KEY: dispatch,
                }
                published_ids.add(prepared.rq_job_id)
                old_published_task_ids.append(prepared.task.id)

            pending = await operations.prepare_admin_operation(
                db,
                operation_type="admin-gitllery-verify",
                scope_key=f"gitllery:verify:{uuid4()}",
                title="Later lost publication",
                entity="gitllery-verify",
                options={"repository_id": str(uuid4())},
                queue_name="maintenance",
                job_timeout=60,
            )
            pending.task.created_at = base + timedelta(minutes=10)
            pending_dispatch = dict(
                pending.task.meta[operations.ADMIN_DISPATCH_META_KEY]
            )
            pending_dispatch["prepared_at"] = base.isoformat()
            pending.task.meta = {
                **pending.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: pending_dispatch,
            }
            pending_id = pending.task.id
            pending_rq_job_id = pending.rq_job_id
            await db.commit()

        recovered = await operations.recover_admin_operation_dispatches(
            now=datetime.now(timezone.utc),
            grace_seconds=0,
            limit=25,
            include_published=True,
        )

        assert recovered == {
            "scanned": 25,
            "published": 25,
            "deferred": 0,
            "failed": 0,
        }
        assert enqueued_ids == [pending_rq_job_id]
        async with async_session() as verify_db:
            pending_task = await verify_db.get(TaskRun, pending_id)
            assert (
                pending_task.meta[operations.ADMIN_DISPATCH_META_KEY][
                    "publication_state"
                ]
                == operations.ADMIN_DISPATCH_PUBLISHED
            )
            probed = 0
            for task_id in old_published_task_ids:
                task = await verify_db.get(TaskRun, task_id)
                dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
                if dispatch.get("next_probe_at"):
                    probed += 1
            assert probed == 24
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordinary_rq_exception_keeps_committed_start_pending(monkeypatch):
    """An ordinary RQ implementation error is deferred after durable admission."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.redis_client import get_redis

    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rq serializer exploded")
        ),
    )
    task_id = None
    try:
        result = await operations.start_admin_operation(
            operation_type="admin-search-reindex",
            scope_key="library:search-reindex:active",
            title="Search reindex",
            entity="search-reindex",
            options={},
            queue_name="maintenance",
            job_timeout=60,
            redis_client=get_redis(),
        )
        task_id = UUID(result["task_id"])
        assert result["publication_state"] == operations.ADMIN_DISPATCH_PENDING
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PENDING
            assert dispatch["last_error_type"] == "RuntimeError"
            assert dispatch["last_error"] == "rq serializer exploded"
            assert dispatch["next_retry_at"] is not None
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_starts_retry_handoff_and_callbacks_are_attempt_fenced():
    """Database arbitration admits one scope owner and one retry attempt."""
    from fastapi import HTTPException

    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    async def prepare_one():
        async with async_session() as db:
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=7 * 24 * 60 * 60,
            )
            await db.commit()
            return prepared.task.id

    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        starts = await asyncio.gather(prepare_one(), prepare_one(), return_exceptions=True)
        admitted = [value for value in starts if not isinstance(value, BaseException)]
        conflicts = [value for value in starts if isinstance(value, HTTPException)]
        assert len(admitted) == 1
        assert len(conflicts) == 1
        assert conflicts[0].status_code == 409
        task_id = admitted[0]

        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            task.status = "failed"
            await db.commit()

        async def retry_one():
            return await operations.prepare_admin_operation_retry(task_id)

        retries = await asyncio.gather(retry_one(), retry_one(), return_exceptions=True)
        admitted_retries = [value for value in retries if not isinstance(value, BaseException)]
        retry_conflicts = [value for value in retries if isinstance(value, HTTPException)]
        assert len(admitted_retries) == 1
        assert len(retry_conflicts) == 1
        assert admitted_retries[0].attempt == 2

        # The retry intent is committed before its RQ handoff. Tokenless and
        # old-attempt callbacks cannot mutate the newly current attempt.
        assert await operations.update_admin_task(
            task_id,
            None,
            status="complete",
            result={"wrong": "tokenless"},
        ) is False
        assert await operations.update_admin_task(
            task_id,
            1,
            status="complete",
            result={"wrong": "old"},
        ) is False
        assert await operations.update_admin_task(
            task_id,
            2,
            status="running",
            progress={"phase": "running"},
        ) is True

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.status == "running"
            assert current.result_data == {}
            assert current.attempts == 2
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_rq_record_repairs_worker_finally_crash():
    """A terminal RQ record closes a TaskRun left running by a crashed callback."""
    from rq import Queue, SimpleWorker

    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            await db.commit()

        queue = Queue(name="maintenance", connection=redis_client)
        queue.enqueue(
            "operator.truediv",
            1,
            0,
            job_id=prepared.rq_job_id,
            result_ttl=300,
            failure_ttl=300,
        )
        worker = SimpleWorker([queue], connection=redis_client)
        assert worker.work(burst=True) is True

        repaired = await operations.recover_admin_operation_dispatches(
            redis_client=redis_client,
            grace_seconds=0,
            include_published=True,
        )
        assert repaired["failed"] == 1
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.status == "failed"
            assert task.reason_code == "rq_terminal_without_callback"
            assert "RQ attempt ended" in task.error_log
    finally:
        if task_id is not None:
            _clear_rq_task(redis_client, task_id)
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finished_rq_record_cannot_authorize_taskrun_success(monkeypatch):
    """Redis transport completion is not a business terminal callback."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    class FinishedJob:
        result = {"status": "failed", "message": "semantic failure"}

        @staticmethod
        def get_status(refresh=True):
            assert refresh is True
            return "finished"

    task_id = None
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: FinishedJob())
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["publication_state"] = operations.ADMIN_DISPATCH_PUBLISHED
            prepared.task.meta = {
                **prepared.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: dispatch,
            }
            await db.commit()

        assert await operations.publish_admin_operation(task_id, 1) == "failed"
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.status == "failed"
            assert task.reason_code == "rq_terminal_without_callback"
            assert task.result_data == {}
            assert "without a current TaskRun callback" in task.error_log
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_running_attempt_cannot_be_claimed_twice():
    """A duplicate delivery of one deterministic RQ id is fenced before work."""
    from app.database import async_session, engine
    from app.services import operations

    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        assert await operations.claim_admin_operation(task_id, 1) == (
            "admin-search-reindex",
            {},
        )
        with pytest.raises(RuntimeError, match="cannot run from running"):
            await operations.claim_admin_operation(task_id, 1)
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_rq_record_never_republishes_a_running_attempt(monkeypatch):
    """Redis record loss cannot create a concurrent executor for one attempt."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    enqueued: list[str] = []
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)
    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_a, rq_job_id, **_k: enqueued.append(rq_job_id),
    )
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            prepared.task.last_heartbeat_at = datetime.now(timezone.utc)
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["publication_state"] = operations.ADMIN_DISPATCH_PUBLISHED
            prepared.task.meta = {
                **prepared.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: dispatch,
            }
            await db.commit()

        assert await operations.publish_admin_operation(task_id, 1) == "active"
        assert enqueued == []
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.status == "running"
            assert task.attempts == 1
            assert task.rq_job_id == f"admin-{task_id}-attempt-1"
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_running_attempt_rotates_before_missing_rq_is_republished(
    monkeypatch,
):
    """Recovery publishes a new attempt only after the PostgreSQL lease expires."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    enqueued: list[tuple[str, int, str]] = []
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)

    def record_enqueue(task_id, attempt, *, rq_job_id, **_kwargs):
        enqueued.append((str(task_id), int(attempt), rq_job_id))
        return SimpleNamespace(id=rq_job_id)

    monkeypatch.setattr(operations, "_enqueue_admin_rq", record_enqueue)
    task_id = None
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            prepared.task.last_heartbeat_at = now - timedelta(minutes=5)
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch.update(
                {
                    "publication_state": operations.ADMIN_DISPATCH_PUBLISHED,
                    "prepared_at": (now - timedelta(minutes=10)).isoformat(),
                    "next_probe_at": (now - timedelta(seconds=1)).isoformat(),
                }
            )
            prepared.task.meta = {
                **prepared.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: dispatch,
            }
            await db.commit()

        report = await operations.recover_admin_operation_dispatches(
            now=now,
            grace_seconds=0,
            include_published=True,
        )
        assert report == {
            "scanned": 1,
            "published": 1,
            "deferred": 0,
            "failed": 0,
        }
        assert enqueued == [
            (str(task_id), 2, f"admin-{task_id}-attempt-2"),
        ]
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert task.attempts == 2
            assert task.last_heartbeat_at is None
            assert dispatch["attempt"] == 2
            assert dispatch["recovered_from_attempt"] == 1
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PUBLISHED
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_recovery_waits_for_postgresql_execution_lease(monkeypatch):
    """A live executor cannot be replaced merely because its RQ record vanished."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    enqueued: list[str] = []
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)
    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_a, rq_job_id, **_k: enqueued.append(rq_job_id),
    )
    task_id = None
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            prepared.task.last_heartbeat_at = now - timedelta(minutes=5)
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch.update(
                {
                    "publication_state": operations.ADMIN_DISPATCH_PUBLISHED,
                    "next_probe_at": (now - timedelta(seconds=1)).isoformat(),
                }
            )
            prepared.task.meta = {
                **prepared.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: dispatch,
            }
            await db.commit()

        async with operations.admin_operation_execution_lease(task_id):
            report = await operations.recover_admin_operation_dispatches(
                now=now + timedelta(seconds=10),
                grace_seconds=0,
                include_published=True,
            )
            assert report == {
                "scanned": 1,
                "published": 0,
                "deferred": 0,
                "failed": 0,
            }
            assert enqueued == []
            async with async_session() as verify_db:
                task = await verify_db.get(TaskRun, task_id)
                assert task.status == "running"
                assert task.attempts == 1

        recovered = await operations.recover_admin_operation_dispatches(
            now=now + timedelta(minutes=1),
            grace_seconds=0,
            include_published=True,
        )
        assert recovered["published"] == 1
        assert enqueued == [f"admin-{task_id}-attempt-2"]
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execution_lease_fences_replacement_task_in_same_scope():
    """Cancelling a TaskRun cannot admit a concurrent worker for its scope."""
    from app.database import async_session, engine
    from app.services import operations

    first_id = None
    second_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            first = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="First search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            first_id = first.task.id
            first.task.status = "cancelled"
            await db.commit()

        async with async_session() as db:
            second = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Replacement search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            second_id = second.task.id
            await db.commit()

        async with operations.admin_operation_execution_lease(first_id):
            with pytest.raises(
                operations.AdminOperationAttemptRejected,
                match="live execution lease",
            ):
                async with operations.admin_operation_execution_lease(second_id):
                    pytest.fail("replacement acquired a concurrently held scope")
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_cancels_handler_when_execution_lease_heartbeat_is_lost(
    monkeypatch,
):
    """Losing the PostgreSQL execution lease stops the old business handler."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.services import operations

    handler_started = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def blocking_handler(*_args, **_kwargs):
        handler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()

    async def lose_lease(_task_id, _attempt, _stop, _lease_db=None):
        await handler_started.wait()
        return "PostgreSQL execution lease was lost"

    monkeypatch.setattr(
        admin_operations,
        "_execute_registered_admin_operation",
        blocking_handler,
    )
    monkeypatch.setattr(
        admin_operations,
        "_heartbeat_registered_admin_operation",
        lose_lease,
    )
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        with pytest.raises(
            operations.AdminOperationAttemptRejected,
            match="execution lease was lost",
        ):
            await asyncio.wait_for(
                admin_operations._run_registered_admin_operation(str(task_id), 1),
                timeout=2,
            )
        assert handler_cancelled.is_set()
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_operation_reads_are_postgresql_first_when_redis_is_down(monkeypatch):
    """Current operation detail and list remain available without Redis."""
    from app.api.admin import data as data_api
    from app.database import async_session, engine
    from app.services import operations

    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        monkeypatch.setattr(
            data_api,
            "get_redis",
            lambda: (_ for _ in ()).throw(redis_lib.ConnectionError("redis down")),
        )
        detail = await data_api.get_admin_operation(str(task_id))
        listing = await data_api.list_active_operations()
        assert detail["job_id"] == str(task_id)
        assert detail["status"] == "enqueued"
        assert [item["job_id"] for item in listing["operations"]] == [str(task_id)]
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_direct_data_admin_starts_use_durable_registry_during_redis_outage(
    monkeypatch,
):
    """Clear, rebuild, disk import, and creator refresh commit pending TaskRuns."""
    from app.api.admin import data as data_api
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    class UnavailableRedis:
        def __getattr__(self, _name):
            raise redis_lib.ConnectionError("redis unavailable")

    unavailable = UnavailableRedis()
    monkeypatch.setattr(data_api, "get_redis", lambda: unavailable)
    monkeypatch.setattr(operations, "get_redis", lambda: unavailable)

    async def clear_start():
        async with async_session() as db:
            return await data_api.start_clear_operation(
                data_api.ClearOperationRequest(entity="tags", confirmation="DELETE-TAGS"),
                db,
            )

    async def disk_start():
        async with async_session() as db:
            return await data_api.import_from_disk(
                data_api.ImportFromDiskRequest(source="pixiv"),
                db,
            )

    starts = (
        clear_start,
        lambda: data_api.rebuild_library(data_api.RebuildLibraryRequest()),
        disk_start,
        data_api.reenrich_creators,
    )
    operation_types = (
        "admin-clear",
        "admin-rebuild",
        "admin-disk-import",
        "admin-creator-reenrich",
    )
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        for start, operation_type in zip(starts, operation_types, strict=True):
            response = await start()
            assert response["status"] == "enqueued"
            assert response["operation_type"] == operation_type
            async with async_session() as db:
                task = await db.get(TaskRun, UUID(response["task_id"]))
                dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
                assert task.status == "enqueued"
                assert dispatch["publication_state"] == "pending"
                task.status = "cancelled"
                await db.commit()
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cleaning_danbooru_and_job_diagnostic_starts_are_durable_without_redis(
    monkeypatch,
):
    """Remaining generic admin publishers commit TaskRun intents first."""
    from app.api import reference as reference_api
    from app.api.admin import data as data_api
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.jobs import import_runner
    from app.models import TaskRun
    from app.services import operations

    class UnavailableRedis:
        def __getattr__(self, _name):
            raise redis_lib.ConnectionError("redis unavailable")

    unavailable = UnavailableRedis()
    monkeypatch.setattr(operations, "get_redis", lambda: unavailable)
    monkeypatch.setattr(reference_api, "get_redis", lambda: unavailable)
    monkeypatch.setattr(settings_api, "get_redis", lambda: unavailable)
    monkeypatch.setattr(
        import_runner,
        "cleanup_metadata_jsons",
        lambda _root: asyncio.sleep(0, result=7),
    )
    monkeypatch.setattr(
        reference_api,
        "_precheck_pixiv_ids",
        lambda _ids: asyncio.sleep(
            0,
            result={
                "duplicates_removed": 0,
                "existing_ids": set(),
                "unique_ids": ["123"],
                "already_exists": [],
            },
        ),
    )

    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)

        responses = [
            await data_api.cleanup_metadata_jsons(),
            await reference_api.import_all_danbooru_async({"pixiv_id": "123"}),
            await reference_api.batch_import_danbooru_artists({"pixiv_ids": ["123"]}),
            await reference_api.url_batch_import_danbooru(
                {"urls": ["https://www.pixiv.net/users/123"]}
            ),
            await settings_api._enqueue_download_conflict_reconciliation(),
        ]
        expected = [
            "admin-cleanup-metadata-jsons",
            "danbooru-import-all",
            "admin-danbooru-batch-import",
            "admin-danbooru-url-batch-import",
            "admin-download-conflict-reconciliation",
        ]
        assert [response["operation_type"] for response in responses] == expected

        async with async_session() as db:
            tasks = list(
                (
                    await db.execute(
                        select(TaskRun).where(TaskRun.operation_type.in_(expected))
                    )
                ).scalars()
            )
            assert {task.operation_type for task in tasks} == set(expected)
            assert all(task.status == "enqueued" for task in tasks)
            assert all(
                task.meta[operations.ADMIN_DISPATCH_META_KEY]["publication_state"]
                == "pending"
                for task in tasks
            )
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_worker_loads_options_and_fences_terminal_write(monkeypatch):
    """RQ receives no options; its worker reloads them and finalizes attempt 1."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import operations

    observed = {}

    async def execute(operation_type, task_id, attempt, options):
        observed.update(
            operation_type=operation_type,
            task_id=task_id,
            attempt=attempt,
            options=options,
        )
        return {"indexed": 5}

    monkeypatch.setattr(
        admin_operations,
        "_execute_registered_admin_operation",
        execute,
    )
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={"reason": "test"},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        result = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_id),
            1,
        )
        assert result == {"indexed": 5}
        assert observed == {
            "operation_type": "admin-search-reindex",
            "task_id": str(task_id),
            "attempt": 1,
            "options": {"reason": "test"},
        }
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.status == "complete"
            assert task.result_data == {"indexed": 5}

        # A redelivery from an old attempt cannot even enter business code.
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            task.status = "failed"
            await db.commit()
        await operations.prepare_admin_operation_retry(task_id)
        observed.clear()
        with pytest.raises(RuntimeError, match="no longer current"):
            await asyncio.to_thread(
                admin_operations.run_registered_admin_operation,
                str(task_id),
                1,
            )
        assert observed == {}
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["jobs", "all"])
async def test_registered_clear_preserves_current_and_other_active_authorities(
    entity,
    monkeypatch,
):
    """Clear jobs/all deletes terminal history without deleting live TaskRuns."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import admin_data, operations
    from app.services.tasks import TaskService

    monkeypatch.setattr(admin_data, "_clear_files", lambda _paths: None)
    monkeypatch.setattr(
        admin_data,
        "_clear_search_index",
        lambda _db, _entity: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        admin_data,
        "clear_failed_rq_jobs",
        lambda _db: asyncio.sleep(0, result=0),
    )
    monkeypatch.setattr(
        admin_data,
        "invalidate_api_caches",
        lambda *_domains: {},
    )
    monkeypatch.setattr(
        admin_data,
        "invalidate_creator_subscription_caches",
        lambda **_kwargs: None,
    )
    task_id = None
    active_id = None
    history_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-clear",
                scope_key=f"library:clear:{entity}",
                title=f"Clear {entity}",
                entity=entity,
                options={"entity": entity},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            service = TaskService(db)
            active = await service.create_task(
                kind="admin",
                operation_type="admin-search-reindex",
                title="Other active authority",
                status="enqueued",
            )
            history = await service.create_task(
                kind="admin",
                operation_type="admin-search-reindex",
                title="Terminal history",
                status="complete",
            )
            active_id = active.id
            history_id = history.id
            await db.commit()

        result = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_id),
            1,
        )

        assert result["status"] == "ok"
        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            active = await verify_db.get(TaskRun, active_id)
            history = await verify_db.get(TaskRun, history_id)
            assert current is not None
            assert current.status == "complete"
            assert active is not None
            assert active.status == "enqueued"
            assert history is None
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_clear_rolls_back_when_attempt_is_superseded(monkeypatch):
    """Destructive clear commits are fenced by the current TaskRun attempt."""
    from app.database import async_session, engine
    from app.models import Tag
    from app.services import admin_data, operations

    monkeypatch.setattr(admin_data, "invalidate_api_caches", lambda *_domains: {})
    task_id = None
    tag_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            tag = Tag(normalized_name=f"fenced-clear-{uuid4()}", category="general")
            db.add(tag)
            await db.flush()
            tag_id = tag.id
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-clear",
                scope_key="library:clear:tags",
                title="Clear tags",
                entity="tags",
                options={"entity": "tags"},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["attempt"] = 2
            prepared.task.attempts = 2
            prepared.task.meta = {
                **prepared.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: dispatch,
            }
            await db.commit()

        with operations.admin_operation_attempt_context(task_id, 1):
            async with async_session() as db:
                with pytest.raises(
                    operations.AdminOperationAttemptRejected,
                    match="no longer current",
                ):
                    await admin_data.clear_entity_data("tags", db)

        async with async_session() as verify_db:
            assert await verify_db.get(Tag, tag_id) is not None
    finally:
        async with async_session() as db:
            if tag_id is not None:
                tag = await db.get(Tag, tag_id)
                if tag is not None:
                    await db.delete(tag)
                    await db.commit()
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_rebuild_persists_awaited_progress_without_redis(
    monkeypatch,
):
    """A rebuild progress callback durably updates TaskRun before completion."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import admin_data, operations, redis_client

    observed_progress: list[dict] = []

    async def rebuild(db, options, progress_callback):
        del options
        await db.commit()
        await progress_callback(
            {
                "phase": "running",
                "scanned": 7,
                "total": 11,
                "metadata_written": 3,
            }
        )
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            observed_progress.append(dict(task.progress_data))
        return {
            "status": "ok",
            "message": "Rebuild complete",
            "scanned": 7,
            "metadata_written": 3,
        }

    monkeypatch.setattr(admin_data, "rebuild_library_index", rebuild)
    monkeypatch.setattr(
        redis_client,
        "get_redis",
        lambda: (_ for _ in ()).throw(redis_lib.ConnectionError("redis down")),
    )
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-rebuild",
                scope_key="library:rebuild:active",
                title="Library rebuild",
                entity="library",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        result = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_id),
            1,
        )

        assert result["status"] == "ok"
        assert observed_progress == [
            {
                "phase": "running",
                "scanned": 7,
                "total": 11,
                "metadata_written": 3,
                "label": "Scanned 7 of 11",
            }
        ]
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.status == "complete"
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_curation_backfill_leaves_terminal_write_to_outer(
    monkeypatch,
):
    """A legacy-shaped handler must return while its current TaskRun is active."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import operations
    from app.services.curation import CurationService

    result = {
        "created": {"creators": 1, "repositories": 2, "work_groups": 3},
        "message": "Curation baseline complete",
    }

    async def run_backfill(_self, *, resource_owner):
        assert resource_owner == str(task_id)
        return result

    monkeypatch.setattr(CurationService, "run_backfill", run_backfill)
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-curation-backfill",
                scope_key="library:curation-backfill:active",
                title="Curation baseline",
                entity="curation-backfill",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        returned = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_id),
            1,
        )

        assert returned == result
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            assert task.status == "complete"
            assert task.result_data == result
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_retry_waits_for_running_worker_execution_lease(monkeypatch):
    """Retry cannot replace a worker until its PostgreSQL lease is released."""
    from fastapi import HTTPException

    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import admin_data, operations

    entered = threading.Event()
    resume = threading.Event()

    async def rebuild(_db, _options, _progress_callback):
        entered.set()
        assert await asyncio.to_thread(resume.wait, 5)
        return {"status": "ok", "message": "attempt one completed"}

    monkeypatch.setattr(admin_data, "rebuild_library_index", rebuild)
    task_id = None
    worker = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-rebuild",
                scope_key="library:rebuild:active",
                title="Library rebuild",
                entity="library",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        worker = asyncio.create_task(
            asyncio.to_thread(
                admin_operations.run_registered_admin_operation,
                str(task_id),
                1,
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        async with async_session() as db:
            task = await db.get(TaskRun, task_id)
            task.status = "failed"
            await db.commit()

        with pytest.raises(HTTPException) as conflict:
            await operations.prepare_admin_operation_retry(task_id)
        assert conflict.value.status_code == 409
        assert "still executing" in str(conflict.value.detail)

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.attempts == 1
            assert current.status == "failed"

        resume.set()
        with pytest.raises(RuntimeError, match="no longer current"):
            await worker

        retry = await operations.prepare_admin_operation_retry(task_id)
        assert retry.attempt == 2
        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.attempts == 2
            assert current.status == "enqueued"
            assert current.progress_data["phase"] == "enqueued"
    finally:
        resume.set()
        if worker is not None and not worker.done():
            await asyncio.gather(worker, return_exceptions=True)
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_creator_refresh_persists_awaited_progress(monkeypatch):
    """Creator refresh progress is a fenced TaskRun update, not a Redis write."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models import TaskRun
    from app.services import creator_enrichment, operations

    observed_progress: list[dict] = []

    async def reenrich(_db, *, progress_cb):
        await progress_cb(
            {
                "scanned": 4,
                "total": 8,
                "found": 0,
                "not_found": 4,
                "errors": 0,
                "skipped": 0,
            }
        )
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            observed_progress.append(dict(task.progress_data))
        return {
            "scanned": 4,
            "total": 8,
            "found": 0,
            "not_found": 4,
            "errors": 0,
            "skipped": 0,
            "aborted": False,
            "items": [],
        }

    monkeypatch.setattr(creator_enrichment, "reenrich_pending", reenrich)
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-creator-reenrich",
                scope_key="library:creator-reenrich:active",
                title="Creator refresh",
                entity="creator-reenrich",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        result = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_id),
            1,
        )

        assert result["not_found"] == 4
        assert observed_progress == [
            {
                "scanned": 4,
                "total": 8,
                "found": 0,
                "not_found": 4,
                "errors": 0,
                "skipped": 0,
                "label": "Mapped 0 of 4 scanned",
            }
        ]
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nested_worker_progress_write_rechecks_current_attempt():
    """Business-handler TaskService writes cannot cross a retry handoff."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations
    from app.services.tasks import TaskService

    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        with operations.admin_operation_attempt_context(task_id, 1):
            async with async_session() as db:
                task = await db.get(TaskRun, task_id)
                await TaskService(db).update_task(
                    task,
                    status="failed",
                    progress={"phase": "attempt-one-failed"},
                )
                await db.commit()

        retry = await operations.prepare_admin_operation_retry(task_id)
        assert retry.attempt == 2

        with operations.admin_operation_attempt_context(task_id, 1):
            async with async_session() as stale_db:
                stale = await stale_db.get(TaskRun, task_id)
                with pytest.raises(
                    operations.AdminOperationAttemptRejected,
                    match="no longer current",
                ):
                    await TaskService(stale_db).update_task(
                        stale,
                        progress={"phase": "stale-progress"},
                    )

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.status == "enqueued"
            assert current.progress_data["phase"] == "enqueued"
            assert current.attempts == 2
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_dedup_worker_uses_postgresql_scope_without_redis_lock(
    monkeypatch,
    tmp_path,
):
    """A registry-owned dedup scan does not depend on the removed Redis lock."""
    from app.database import async_session, engine
    from app.jobs import admin_operations, asset_dedup
    from app.models import Asset, AssetDedupScan, TaskRun
    from app.services import operations

    class UnavailableRedis:
        def __getattr__(self, _name):
            raise redis_lib.ConnectionError("redis unavailable")

    task_id = None
    try:
        monkeypatch.setenv(
            "HEAVY_IO_LOCK_PATH",
            str(tmp_path / "locks" / "heavy-io.lock"),
        )
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            cursor_asset_id = (
                await db.execute(select(Asset.id).order_by(Asset.id.desc()).limit(1))
            ).scalar_one_or_none()
            scan = AssetDedupScan(
                status="pending",
                cursor_asset_id=cursor_asset_id,
                options={"auto_apply": False, "batch_size": 1},
            )
            db.add(scan)
            await db.flush()
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Asset dedup scan",
                entity="assets",
                options={"scan_id": str(scan.id)},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            scan_id = scan.id
            await db.commit()

        monkeypatch.setattr(asset_dedup, "get_redis", lambda: UnavailableRedis())
        result = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_id),
            1,
        )
        assert result["status"] == "complete"
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            scan = await verify_db.get(AssetDedupScan, scan_id)
            assert task.status == "complete"
            assert scan.status == "complete"
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_dedup_failed_result_is_one_retryable_terminal_transition(
    monkeypatch,
):
    """A semantic dedup failure must never be overwritten by outer completion."""
    from app.database import async_session, engine
    from app.jobs import admin_operations, asset_dedup
    from app.models import AssetDedupScan, TaskEvent, TaskRun
    from app.services import operations

    scan_id = None
    failure_result = {
        "scan_id": str(scan_id),
        "status": "failed",
        "assets_scanned": 7,
        "candidates_evaluated": 3,
        "cases_created": 1,
        "assets_grouped": 0,
        "bytes_reclaimable": 0,
        "resource_state": "yielded",
        "resource_reason": "scan_slice_failed",
        "successor_delay_seconds": 0.0,
        "generation": 0,
    }

    async def failed_handler(task_id: str, attempt: int, options: dict) -> dict:
        assert attempt == 1
        assert options == {"scan_id": str(scan_id)}
        assert UUID(task_id) == task_uuid
        return dict(failure_result)

    monkeypatch.setattr(
        asset_dedup,
        "run_registered_asset_dedup_scan",
        failed_handler,
    )
    task_uuid = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            scan = AssetDedupScan(
                status="failed",
                options={"auto_apply": False, "batch_size": 1},
                error="asset dedup scan failed",
            )
            db.add(scan)
            await db.flush()
            scan_id = scan.id
            failure_result["scan_id"] = str(scan_id)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Asset dedup scan",
                entity="assets",
                options={"scan_id": str(scan_id)},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_uuid = prepared.task.id
            await db.commit()

        returned = await asyncio.to_thread(
            admin_operations.run_registered_admin_operation,
            str(task_uuid),
            1,
        )
        assert returned == failure_result

        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_uuid)
            events = list(
                (
                    await verify_db.execute(
                        select(TaskEvent)
                        .where(TaskEvent.task_run_id == task_uuid)
                        .order_by(TaskEvent.id)
                    )
                ).scalars()
            )
            assert task.status == "failed"
            assert task.reason_code == "operation_semantic_failure"
            assert task.result_data == failure_result
            assert task.error_log == "asset dedup scan failed"
            assert [
                (event.from_status, event.to_status)
                for event in events
                if event.to_status == "failed"
            ] == [("running", "failed")]
            assert all(event.to_status != "complete" for event in events)

        retry = await operations.prepare_admin_operation_retry(task_uuid)
        assert retry.attempt == 2
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_uuid)
            failed_events = list(
                (
                    await verify_db.execute(
                        select(TaskEvent).where(
                            TaskEvent.task_run_id == task_uuid,
                            TaskEvent.to_status == "failed",
                        )
                    )
                ).scalars()
            )
            assert task.status == "enqueued"
            assert task.attempts == 2
            assert len(failed_events) == 1
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            if scan_id is not None:
                scan = await db.get(AssetDedupScan, scan_id)
                if scan is not None:
                    await db.delete(scan)
                    await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_dedup_retry_resets_failed_scan_before_new_attempt():
    """Retry rotates TaskRun and restores the dedup domain cursor atomically."""
    from app.database import async_session, engine
    from app.models import AssetDedupScan, TaskRun
    from app.services import operations

    task_id = None
    scan_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            scan = AssetDedupScan(
                status="failed",
                options={
                    "auto_apply": False,
                    "batch_size": 1,
                    "_rq_generation": 3,
                },
                error="image derive failed",
            )
            db.add(scan)
            await db.flush()
            scan_id = scan.id
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Asset dedup scan",
                entity="assets",
                options={"scan_id": str(scan.id), "_scan_generation": 3},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "failed"
            prepared.task.error_log = "image derive failed"
            await db.commit()

        retry = await operations.prepare_admin_operation_retry(task_id)
        assert retry.attempt == 2
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            scan = await verify_db.get(AssetDedupScan, scan_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert scan.status == "pending"
            assert scan.error is None
            assert dispatch["options"]["scan_id"] == str(scan_id)
            assert dispatch["options"]["_scan_generation"] == 3
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            if scan_id is not None:
                scan = await db.get(AssetDedupScan, scan_id)
                if scan is not None:
                    await db.delete(scan)
                    await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_registered_dedup_retry_recovers_running_scan_after_worker_crash(
    monkeypatch,
):
    """RQ terminal repair leaves a running cursor resumable by the next attempt."""
    from app.database import async_session, engine
    from app.models import AssetDedupScan, TaskRun
    from app.services import operations

    class FailedJob:
        @staticmethod
        def get_status(refresh=True):
            assert refresh is True
            return "failed"

    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: FailedJob())
    task_id = None
    scan_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            scan = AssetDedupScan(
                status="running",
                options={"_rq_generation": 4, "_operation_job_id": "old-attempt"},
            )
            db.add(scan)
            await db.flush()
            scan_id = scan.id
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Asset dedup scan",
                entity="assets",
                options={"scan_id": str(scan.id), "_scan_generation": 4},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.status = "running"
            dispatch = dict(prepared.task.meta[operations.ADMIN_DISPATCH_META_KEY])
            dispatch["publication_state"] = operations.ADMIN_DISPATCH_PUBLISHED
            prepared.task.meta = {
                **prepared.task.meta,
                operations.ADMIN_DISPATCH_META_KEY: dispatch,
            }
            await db.commit()

        assert await operations.publish_admin_operation(task_id, 1) == "failed"
        retry = await operations.prepare_admin_operation_retry(task_id)
        assert retry.attempt == 2
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            scan = await verify_db.get(AssetDedupScan, scan_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert scan.status == "pending"
            assert scan.error is None
            assert dispatch["options"]["_scan_generation"] == 4
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            if scan_id is not None:
                scan = await db.get(AssetDedupScan, scan_id)
                if scan is not None:
                    await db.delete(scan)
                    await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_handoff_is_committed_before_successor_publication():
    """A bounded worker handoff survives loss before any Redis publication."""
    from app.database import async_session, engine
    from app.models import TaskRun
    from app.services import operations

    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="asset-dedup-scan",
                scope_key="lock:admin:asset-dedup-scan",
                title="Asset dedup scan",
                entity="assets",
                options={"scan_id": str(uuid4()), "_scan_generation": 0},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        async with async_session() as handoff_db:
            handoff = await operations.prepare_admin_operation_handoff(
                handoff_db,
                task_id,
                1,
                options={"scan_id": "durable", "_scan_generation": 1},
                delay_seconds=45,
                progress={"phase": "waiting", "current": 10},
            )
            await handoff_db.commit()

        assert handoff is not None
        assert handoff.attempt == 2
        assert handoff.rq_job_id == f"admin-{task_id}-attempt-2"
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert task.attempts == 2
            assert dispatch["publication_state"] == "pending"
            assert dispatch["options"]["_scan_generation"] == 1
            assert datetime.fromisoformat(dispatch["next_retry_at"]) >= (
                datetime.now(timezone.utc) + timedelta(seconds=40)
            )
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_recovery_instances_publish_one_deterministic_job():
    """Two recovery loops can race without duplicating one committed attempt."""
    from app.database import async_session, engine
    from app.services import operations
    from app.services.redis_client import get_redis

    redis_client = get_redis()
    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-search-reindex",
                scope_key="library:search-reindex:active",
                title="Search reindex",
                entity="search-reindex",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            await db.commit()

        reports = await asyncio.gather(
            *(
                operations.recover_admin_operation_dispatches(
                    redis_client=redis_client,
                    now=datetime.now(timezone.utc) + timedelta(seconds=31),
                    grace_seconds=0,
                )
                for _ in range(2)
            )
        )
        assert sum(report["published"] for report in reports) >= 1
        queued_ids = [
            value.decode() if isinstance(value, bytes) else value
            for value in redis_client.lrange("rq:queue:maintenance", 0, -1)
        ]
        assert queued_ids.count(prepared.rq_job_id) == 1
    finally:
        if task_id is not None:
            _clear_rq_task(redis_client, task_id)
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_import_guard_follows_registry_attempt_after_retry():
    """The disk heartbeat guard cannot retain its pre-registry attempt token."""
    from app.database import async_session, engine
    from app.jobs.admin_operations import _DiskImportPublisherGuard
    from app.models import TaskRun
    from app.services import operations
    from app.services.publisher_attempts import PUBLISHER_ATTEMPT_META_KEY

    task_id = None
    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-disk-import",
                scope_key="library:disk-import:active",
                title="Import from disk",
                entity="disk-import",
                options={},
                queue_name="maintenance",
                job_timeout=60,
            )
            task_id = prepared.task.id
            prepared.task.meta = {
                **prepared.task.meta,
                PUBLISHER_ATTEMPT_META_KEY: "1",
            }
            prepared.task.status = "failed"
            await db.commit()

        retry = await operations.prepare_admin_operation_retry(task_id)
        assert retry.attempt == 2
        guard = _DiskImportPublisherGuard(str(task_id), "2")
        assert await guard.initialize_attempt() == "2"
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.meta[operations.ADMIN_DISPATCH_META_KEY]["attempt"] == 2
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_admin_retry_is_read_only_and_never_consults_redis(monkeypatch):
    """One-version legacy compatibility permits reads, not new publications."""
    from fastapi import HTTPException

    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
            service = TaskService(db)
            task = await service.create_task(
                kind="admin",
                operation_type="admin-search-reindex",
                title="Legacy search reindex",
                status="failed",
                meta={"entity": "search-reindex"},
            )
            await db.commit()
            monkeypatch.setattr(
                tasks_api,
                "get_redis",
                lambda: (_ for _ in ()).throw(
                    AssertionError("legacy retry must not consult Redis")
                ),
            )
            with pytest.raises(HTTPException) as raised:
                await tasks_api._retry_admin_task(task, service)
            assert raised.value.status_code == 400
            assert "legacy" in str(raised.value.detail).lower()
    finally:
        async with async_session() as db:
            await _clear_dispatch_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_compaction_and_reconciliation_follow_global_row_lock_order():
    """Two real PostgreSQL sessions cannot form TaskRun/DownloadJob inversion."""
    from app.database import async_session, engine
    from app.models import Creator, DownloadJob, StorageArtifact, Subscription
    from app.services.operation_attention import compact_terminal_tasks, reconcile_task_truth
    from app.services.tasks import TaskService

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as setup_db:
            await setup_db.execute(text("""
                TRUNCATE
                    task_events, task_runs, storage_artifacts, import_jobs,
                    download_jobs, subscription_sources, subscriptions, creators
                RESTART IDENTITY CASCADE
            """))
            creator = Creator(name=f"dispatch-lock-order-{uuid4()}")
            setup_db.add(creator)
            await setup_db.flush()
            subscription = Subscription(creator_id=creator.id, name="Lock order")
            setup_db.add(subscription)
            await setup_db.flush()
            download = DownloadJob(
                subscription_id=subscription.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/1",
                status="complete",
            )
            setup_db.add(download)
            await setup_db.flush()
            artifact = StorageArtifact(
                storage_root="downloads",
                file_path=f"lock-order/{uuid4()}.json",
                source="pixiv",
                creator_dir="1",
                source_work_id="1",
                file_name="1.json",
                artifact_type="metadata_json",
                download_job_id=download.id,
                state="done",
            )
            setup_db.add(artifact)
            task = await TaskService(setup_db).ensure_download_task(download)
            task.status = "enqueued"
            task.compactable_at = now - timedelta(minutes=1)
            await setup_db.commit()
            download_id = download.id

        async with async_session() as reconciliation_db, async_session() as compaction_db:
            # Reconciliation's global order has already reached DownloadJob.
            await reconciliation_db.execute(
                select(DownloadJob)
                .where(DownloadJob.id == download_id)
                .with_for_update(of=DownloadJob)
            )
            compaction_pid = int(
                (await compaction_db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            compaction = asyncio.create_task(
                compact_terminal_tasks(compaction_db, dry_run=False, limit=10)
            )

            # Wait until compaction reaches the DownloadJob lock held above.
            for _ in range(100):
                async with async_session() as probe_db:
                    waiting = (
                        await probe_db.execute(
                            text(
                                "SELECT wait_event_type FROM pg_stat_activity "
                                "WHERE pid = :pid"
                            ),
                            {"pid": compaction_pid},
                        )
                    ).scalar_one_or_none()
                if waiting == "Lock":
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail("compaction never reached the held DownloadJob lock")

            reconciliation = asyncio.create_task(
                reconcile_task_truth(reconciliation_db, dry_run=False, limit=10)
            )
            results = await asyncio.wait_for(
                asyncio.gather(compaction, reconciliation, return_exceptions=True),
                timeout=8,
            )
            assert not [result for result in results if isinstance(result, BaseException)]
    finally:
        async with async_session() as db:
            await db.execute(text("""
                TRUNCATE
                    task_events, task_runs, storage_artifacts, import_jobs,
                    download_jobs, subscription_sources, subscriptions, creators
                RESTART IDENTITY CASCADE
            """))
            await db.commit()
        await engine.dispose()
