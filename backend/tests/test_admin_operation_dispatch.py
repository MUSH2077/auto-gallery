"""Durable administrator-operation dispatch and lock-order regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
