import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text


_PUBLISHER_ATTEMPT_META_KEY = "_bounded_import_publisher_attempt"


async def _prepare_registered_admin_task(
    db,
    *,
    operation_type="admin-disk-import",
    scope_key="library:disk-import:active",
    title="Import from disk",
    entity="disk-import",
    options=None,
    status="enqueued",
    task_id=None,
    queue_name="maintenance",
    job_timeout=14400,
):
    from app.services import operations

    prepared = await operations.prepare_admin_operation(
        db,
        operation_type=operation_type,
        scope_key=scope_key,
        title=title,
        entity=entity,
        options=options or {},
        queue_name=queue_name,
        job_timeout=job_timeout,
        task_id=task_id,
    )
    prepared.task.status = status
    return prepared.task


def _record_admin_publications(monkeypatch):
    from app.services import operations

    publications: list[tuple[str, int]] = []

    async def record(task_id, attempt, *, redis_client=None):
        publications.append((str(task_id), int(attempt)))
        return "published"

    monkeypatch.setattr(operations, "publish_admin_operation", record)
    return publications


def test_normalize_task_status_maps_legacy_queued_states():
    from app.services.tasks import normalize_task_status

    assert normalize_task_status("queued") == "enqueued"
    assert normalize_task_status("pending") == "enqueued"
    assert normalize_task_status("downloaded") == "running"
    assert normalize_task_status("importing") == "running"
    assert normalize_task_status("complete") == "complete"


def test_disk_import_completion_progress_keeps_durable_counters():
    from app.jobs.admin_operations import disk_import_completion_progress

    progress = disk_import_completion_progress({
        "jobs": 1,
        "import_job_ids": ["first-import", "second-import"],
        "scanned": 5,
        "existing": 1,
        "imported": 2,
        "skipped": 1,
        "failed": 1,
    })

    assert progress == {
        "phase": "complete",
        "label": "Queued 2 import jobs",
        "scanned": 5,
        "existing": 1,
        "imported": 2,
        "skipped": 1,
        "failed": 1,
    }


def test_disk_import_entrypoint_validates_attempt_before_heavy_io(monkeypatch):
    """Admission may persist resource state, so authority must precede it."""
    from app.jobs import admin_operations
    from app.services.redis_pubsub import PublisherFenceError

    entered_heavy_io = False

    async def reject_stale_attempt(_guard):
        raise PublisherFenceError(
            "disk import publisher attempt is no longer current",
            recovery_won=True,
        )

    async def heavy_io_probe(_workload, _owner, operation_factory):
        nonlocal entered_heavy_io
        entered_heavy_io = True
        return await operation_factory()

    monkeypatch.setattr(
        admin_operations._DiskImportPublisherGuard,
        "initialize_attempt",
        reject_stale_attempt,
    )
    monkeypatch.setattr(admin_operations, "run_heavy_io_operation", heavy_io_probe)

    with pytest.raises(PublisherFenceError, match="no longer current"):
        admin_operations.run_disk_import_operation(
            str(uuid4()),
            {},
            "captured-attempt-a",
        )

    assert entered_heavy_io is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_startup_cannot_replace_rotated_operational_attempt(
    monkeypatch,
):
    """A validated A resuming after B's handoff leaves all Redis state at B."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.publisher_attempts import set_publisher_attempt
    from app.services.redis_client import get_redis
    from app.services.redis_pubsub import PublisherFenceError
    from app.services.tasks import TaskService

    task_id = uuid4()
    attempt_a = uuid4().hex
    attempt_b = uuid4().hex
    redis_client = get_redis()
    entered_a_admission = threading.Event()
    resume_a_admission = threading.Event()
    original_acquire = admin_operations.acquire_operation_lock
    outcome: dict[str, object] = {}

    def pausing_acquire(*args, **kwargs):
        if kwargs.get("publisher_attempt") == attempt_a:
            entered_a_admission.set()
            assert resume_a_admission.wait(5)
        return original_acquire(*args, **kwargs)

    async def should_not_run_heavy_io(*_args, **_kwargs):
        return {"stale_attempt_entered_heavy_io": True}

    monkeypatch.setattr(admin_operations, "acquire_operation_lock", pausing_acquire)
    monkeypatch.setattr(
        admin_operations,
        "run_heavy_io_operation",
        should_not_run_heavy_io,
    )

    def run_attempt_a() -> None:
        try:
            outcome["result"] = admin_operations.run_disk_import_operation(
                str(task_id),
                {},
                attempt_a,
            )
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_attempt_a, name="startup-attempt-a")
    try:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await TaskService(db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="startup attempt A",
                status="enqueued",
                queue_name="maintenance",
                meta={_PUBLISHER_ATTEMPT_META_KEY: attempt_a},
            )
            await db.commit()
        assert operations.acquire_operation_lock(
            redis_client,
            "library:disk-import:active",
            str(task_id),
            ttl_seconds=604800,
            publisher_attempt=attempt_a,
        )

        worker.start()
        assert await asyncio.to_thread(entered_a_admission.wait, 5)

        async with async_session() as rotate_db:
            current = await rotate_db.get(TaskRun, task_id, with_for_update=True)
            set_publisher_attempt(current, attempt_b)
            await rotate_db.commit()
        pipe = redis_client.pipeline(transaction=True)
        pipe.set(
            "library:disk-import:active",
            str(task_id),
            ex=604800,
        )
        pipe.set(
            operations.operation_attempt_key(str(task_id)),
            attempt_b,
            ex=604800,
        )
        pipe.execute()
        operations.set_operation_status(
            str(task_id),
            "enqueued",
            "admin-disk-import",
            progress={"phase": "attempt-b"},
            publisher_attempt=attempt_b,
            redis_client=redis_client,
        )

        resume_a_admission.set()
        await asyncio.to_thread(worker.join, 5)
        assert not worker.is_alive()

        assert isinstance(outcome.get("error"), PublisherFenceError)
        assert "result" not in outcome
        assert operations.current_operation_attempt(
            redis_client,
            str(task_id),
        ) == attempt_b
        assert operations.get_operation_status(str(task_id))["progress"] == {
            "phase": "attempt-b"
        }
        assert redis_client.get("library:disk-import:active") in {
            str(task_id),
            str(task_id).encode(),
        }
        assert operations.acquire_operation_lock(
            redis_client,
            "library:disk-import:active",
            str(uuid4()),
            ttl_seconds=60,
            publisher_attempt=uuid4().hex,
        ) is False
    finally:
        resume_a_admission.set()
        if worker.is_alive():
            await asyncio.to_thread(worker.join, 5)
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initial_disk_import_cannot_reclaim_attempt_owner_before_first_cache(
    monkeypatch,
):
    """B's committed active scope prevents a second disk-import intent."""
    from fastapi import HTTPException

    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine

    task_b = uuid4()
    publications = _record_admin_publications(monkeypatch)

    try:
        async with async_session() as setup_db:
            await _clear_task_test_tables(setup_db)
            await _prepare_registered_admin_task(
                setup_db,
                task_id=task_b,
                title="attempt B publishing",
            )
            await setup_db.commit()

        async with async_session() as request_db:
            with pytest.raises(HTTPException) as failure:
                await data_api.import_from_disk(
                    ImportFromDiskRequest(),
                    request_db,
                )

        assert failure.value.status_code == 409
        assert publications == []
        assert failure.value.detail["task_id"] == str(task_b)
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initial_disk_import_reclaims_cacheless_owner_only_with_terminal_taskrun(
    monkeypatch,
):
    """A new intent is admitted only after durable terminal evidence."""
    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine
    from app.models.task_run import TaskRun

    task_b = uuid4()
    publications = _record_admin_publications(monkeypatch)

    result = None
    try:
        async with async_session() as setup_db:
            await _clear_task_test_tables(setup_db)
            await _prepare_registered_admin_task(
                setup_db,
                task_id=task_b,
                title="terminal attempt B",
                status="failed",
            )
            await setup_db.commit()

        async with async_session() as request_db:
            result = await data_api.import_from_disk(
                ImportFromDiskRequest(),
                request_db,
            )

        assert result["status"] == "enqueued"
        assert result["task_id"] != str(task_b)
        assert publications == [(result["task_id"], 1)]
        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, UUID(result["task_id"]))
            assert current.status == "enqueued"
            assert current.rq_job_id == result["job_id"]
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_cannot_reclaim_other_attempt_owner_before_first_cache(
    monkeypatch,
):
    """Retry C cannot supersede a different committed active scope owner."""
    from fastapi import HTTPException

    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    task_b = uuid4()
    task_c = uuid4()
    publications = _record_admin_publications(monkeypatch)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            service = TaskService(db)
            retry_task = await _prepare_registered_admin_task(
                db,
                task_id=task_c,
                title="independent retry C",
                status="failed",
            )
            await _prepare_registered_admin_task(
                db,
                task_id=task_b,
                title="attempt B publishing",
            )
            await db.commit()

            with pytest.raises(HTTPException) as failure:
                await tasks_api._retry_admin_task(retry_task, service)

        assert failure.value.status_code == 409
        assert publications == []
        assert failure.value.detail["task_id"] == str(task_b)
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_disk_worker_adopts_attempt_owned_operational_pointer(
    monkeypatch,
):
    """A validated rolling delivery versions its UUID-only Redis owner."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.services.operations import current_operation_attempt
    from app.services.redis_client import get_redis
    from app.services.tasks import TaskService

    task_id = uuid4()
    redis_client = get_redis()
    observed_attempts = []

    async def admission_probe(
        _workload,
        owner,
        _operation_factory,
        *,
        publisher_attempt=None,
    ):
        observed_attempts.append(publisher_attempt)
        assert owner == str(task_id)
        assert current_operation_attempt(redis_client, owner) == publisher_attempt
        return {"admitted": True}

    monkeypatch.setattr(
        admin_operations,
        "run_heavy_io_operation",
        admission_probe,
    )

    try:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await TaskService(db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="rolling delivery",
                status="enqueued",
                queue_name="maintenance",
                meta={"entity": "disk-import"},
            )
            await db.commit()
        redis_client.set(
            "library:disk-import:active",
            str(task_id),
            ex=604800,
        )

        result = await asyncio.to_thread(
            admin_operations.run_disk_import_operation,
            str(task_id),
            {},
        )

        assert result == {"admitted": True}
        assert len(observed_attempts) == 1
        assert observed_attempts[0]
    finally:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_import_operation_publishes_real_task_heartbeats(monkeypatch):
    """The durable admin publisher owns the heartbeat used by recovery."""
    # Import this module before patching the redis-client factory below so its
    # module-level compatibility alias cannot retain the test double.
    from app.api import tasks as _tasks_api  # noqa: F401
    from app.jobs import admin_operations, worker_control

    class FakeRedis:
        def __init__(self):
            self.heartbeat_keys: list[str] = []

        def publish(self, _channel, _payload):
            return 1

        def setex(self, key, _ttl, _value):
            if key.endswith(":heartbeat_ts"):
                self.heartbeat_keys.append(key)
            return True

        def eval(self, _script, numkeys, *args):
            if numkeys == 3:
                self.heartbeat_keys.append(args[0])
                return 1
            return 0

    fake_redis = FakeRedis()
    job_id = str(uuid4())

    async def slow_reconcile(
        _db,
        _options,
        _progress,
        *,
        publisher_checkpoint=None,
        publisher_attempt=None,
    ):
        assert publisher_checkpoint is not None
        assert publisher_attempt
        await asyncio.sleep(0.05)
        return {
            "jobs": 0,
            "scanned": 0,
            "existing": 0,
            "imported": 0,
            "skipped": 0,
            "failed": 0,
        }

    monkeypatch.setattr(worker_control, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(
        "app.services.redis_pubsub.get_redis",
        lambda: fake_redis,
    )
    monkeypatch.setattr(
        "app.services.redis_client.get_redis",
        lambda: fake_redis,
    )
    monkeypatch.setattr(
        "app.services.disk_import.reconcile_downloads_to_db",
        slow_reconcile,
    )
    monkeypatch.setattr(admin_operations, "set_operation_status", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.api.admin.settings.invalidate_storage_breakdown_cache",
        lambda: None,
    )

    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await TaskService(db).create_task(
                task_id=UUID(job_id),
                kind="admin",
                operation_type="admin-disk-import",
                title="Heartbeat publisher",
                status="enqueued",
                queue_name="maintenance",
            )
            await db.commit()

        result = await admin_operations._run_disk_import_operation(job_id, {})

        assert result["jobs"] == 0
        assert any(
            key.startswith(f"task:{job_id}:publisher:")
            and key.endswith(":heartbeat_ts")
            for key in fake_redis.heartbeat_keys
        )
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_worker_raised_error_and_logs_redact_private_attempt(
    monkeypatch,
    caplog,
):
    """RQ failure serialization cannot retain authority in exception text."""
    from app.database import async_session, engine
    from app.jobs import admin_operations
    from app.services.operations import acquire_operation_lock
    from app.services.redis_client import get_redis
    from app.services.tasks import TaskService

    task_id = uuid4()
    attempt = uuid4().hex
    redis_client = get_redis()

    async def failing_reconcile(*_args, **_kwargs):
        raise RuntimeError(f"publisher failure included {attempt}")

    monkeypatch.setattr(
        "app.services.disk_import.reconcile_downloads_to_db",
        failing_reconcile,
    )
    caplog.set_level(logging.ERROR)

    try:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await TaskService(db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="private worker failure",
                status="enqueued",
                queue_name="maintenance",
                meta={_PUBLISHER_ATTEMPT_META_KEY: attempt},
            )
            await db.commit()
        assert acquire_operation_lock(
            redis_client,
            "library:disk-import:active",
            str(task_id),
            ttl_seconds=604800,
            publisher_attempt=attempt,
        )

        with pytest.raises(RuntimeError) as failure:
            await admin_operations._run_disk_import_operation(
                str(task_id),
                {},
                attempt,
            )

        assert attempt not in str(failure.value)
        assert attempt not in caplog.text
    finally:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rotated_attempt_cannot_update_between_batch_resource_projection(
    monkeypatch,
):
    """A validated publisher carries its attempt into later resource updates."""
    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.jobs import admin_operations, worker_control
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.operations import set_operation_status
    from app.services.publisher_attempts import current_publisher_attempt
    from app.services.redis_client import get_redis
    from app.services.tasks import TaskService, update_task_resource_state

    redis_client = get_redis()
    task_id = uuid4()
    attempt_a = "1"
    entered_batch_capacity = threading.Event()
    resume_attempt_a = threading.Event()
    records = []
    publications = _record_admin_publications(monkeypatch)

    async def between_batch_probe(
        _db,
        _options,
        _progress,
        *,
        publisher_checkpoint=None,
        publisher_attempt=None,
    ):
        assert publisher_checkpoint is not None
        entered_batch_capacity.set()
        await asyncio.to_thread(resume_attempt_a.wait, 5)
        kwargs = (
            {"publisher_attempt": publisher_attempt}
            if publisher_attempt is not None
            else {}
        )
        await update_task_resource_state(
            str(task_id),
            "running",
            "between_batch_probe",
            **kwargs,
        )
        await publisher_checkpoint(_db, lock_task=True)
        return {
            "jobs": 0,
            "scanned": 0,
            "existing": 0,
            "imported": 0,
            "skipped": 0,
            "failed": 0,
        }

    monkeypatch.setattr(
        "app.services.disk_import.reconcile_downloads_to_db",
        between_batch_probe,
    )
    monkeypatch.setattr(
        worker_control.HeartbeatPublisher,
        "start",
        lambda _self: None,
    )
    monkeypatch.setattr(
        worker_control.HeartbeatPublisher,
        "stop",
        lambda _self: None,
    )
    monkeypatch.setattr(
        "app.api.admin.settings.invalidate_storage_breakdown_cache",
        lambda: None,
    )
    monkeypatch.setattr("rq.Queue", _recording_disk_queue(records))

    worker_result = {}

    def run_attempt_a():
        try:
            worker_result["value"] = asyncio.run(
                admin_operations._run_disk_import_operation(
                    str(task_id),
                    {"source": "pixiv"},
                    attempt_a,
                )
            )
        except BaseException as exc:  # assertion inspects the real stale exit
            worker_result["error"] = exc

    attempt_a_thread = threading.Thread(
        target=run_attempt_a,
        name="between-batch-attempt-a",
    )
    try:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await _prepare_registered_admin_task(
                db,
                task_id=task_id,
                title="between batch A",
                options={"source": "pixiv"},
            )
            await db.commit()
        redis_client.set(
            "library:disk-import:active",
            str(task_id),
            ex=604800,
        )
        set_operation_status(
            str(task_id),
            "enqueued",
            "admin-disk-import",
            meta={"entity": "disk-import", "source": "pixiv"},
        )

        attempt_a_thread.start()
        assert await asyncio.to_thread(entered_batch_capacity.wait, 5)

        async with async_session() as retry_db:
            service = TaskService(retry_db)
            current = await service.get(task_id)
            await service.update_task(current, status="stale")
            await retry_db.commit()
            await tasks_api._retry_admin_task(current, service)
            retry_db.expire_all()
            current = await service.get(task_id)
            attempt_b = current_publisher_attempt(current)
            assert attempt_b and attempt_b != attempt_a
            expected_resource_state = current.resource_state
            assert publications == [(str(task_id), 2)]

        resume_attempt_a.set()
        await asyncio.to_thread(attempt_a_thread.join, 5)
        assert not attempt_a_thread.is_alive()

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.meta[operations.ADMIN_DISPATCH_META_KEY]["attempt"] == 2
            assert current.status == "enqueued"
            assert current.resource_state == expected_resource_state
            assert current.resource_reason is None
    finally:
        resume_attempt_a.set()
        if attempt_a_thread.is_alive():
            await asyncio.to_thread(attempt_a_thread.join, 5)
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_attempt_a_terminal_cache_and_finally_cannot_clobber_running_b(
    monkeypatch,
):
    """B's running cache and single-flight owner survive A's terminal tail."""
    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.jobs import admin_operations, worker_control
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.publisher_attempts import current_publisher_attempt
    from app.services.redis_client import get_redis
    from app.services.tasks import TaskService

    redis_client = get_redis()
    task_id = uuid4()
    attempt_a = "1"
    records = []
    a_terminal_cache = threading.Event()
    resume_a_terminal = threading.Event()
    b_running = threading.Event()
    finish_b = threading.Event()
    reconcile_lock = threading.Lock()
    reconcile_calls = 0
    original_set_status = operations.set_operation_status
    publications = _record_admin_publications(monkeypatch)

    async def interleaved_reconcile(
        _db,
        _options,
        _progress,
        *,
        publisher_checkpoint=None,
        **_kwargs,
    ):
        nonlocal reconcile_calls
        assert publisher_checkpoint is not None
        with reconcile_lock:
            reconcile_calls += 1
            call_number = reconcile_calls
        if call_number == 2:
            b_running.set()
            await asyncio.to_thread(finish_b.wait, 5)
        return {
            "jobs": 0,
            "scanned": 0,
            "existing": 0,
            "imported": 0,
            "skipped": 0,
            "failed": 0,
        }

    def pausing_set_status(job_id, status, operation_type, **kwargs):
        if (
            threading.current_thread().name == "terminal-attempt-a"
            and status == "complete"
        ):
            a_terminal_cache.set()
            assert resume_a_terminal.wait(5)
        return original_set_status(job_id, status, operation_type, **kwargs)

    monkeypatch.setattr(
        "app.services.disk_import.reconcile_downloads_to_db",
        interleaved_reconcile,
    )
    monkeypatch.setattr(admin_operations, "set_operation_status", pausing_set_status)
    monkeypatch.setattr(
        worker_control.HeartbeatPublisher,
        "start",
        lambda _self: None,
    )
    monkeypatch.setattr(
        worker_control.HeartbeatPublisher,
        "stop",
        lambda _self: None,
    )
    monkeypatch.setattr(
        "app.api.admin.settings.invalidate_storage_breakdown_cache",
        lambda: None,
    )
    monkeypatch.setattr("rq.Queue", _recording_disk_queue(records))

    thread_results = {}

    def run_worker(name, attempt):
        try:
            thread_results[name] = asyncio.run(
                admin_operations._run_disk_import_operation(
                    str(task_id),
                    {"source": "pixiv"},
                    attempt,
                )
            )
        except BaseException as exc:
            thread_results[f"{name}_error"] = exc

    thread_a = threading.Thread(
        target=run_worker,
        args=("a", attempt_a),
        name="terminal-attempt-a",
    )
    thread_b = None
    try:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await _prepare_registered_admin_task(
                db,
                task_id=task_id,
                title="terminal attempt A",
                options={"source": "pixiv"},
            )
            await db.commit()
        redis_client.set("library:disk-import:active", str(task_id), ex=604800)
        original_set_status(
            str(task_id),
            "enqueued",
            "admin-disk-import",
            meta={"entity": "disk-import", "source": "pixiv"},
        )

        thread_a.start()
        assert await asyncio.to_thread(a_terminal_cache.wait, 5)

        async with async_session() as retry_db:
            service = TaskService(retry_db)
            completed_a = await service.get(task_id)
            assert completed_a.status == "complete"
            await service.update_task(completed_a, status="stale")
            await retry_db.commit()
            await tasks_api._retry_admin_task(completed_a, service)
            retry_db.expire_all()
            completed_a = await service.get(task_id)
            attempt_b = current_publisher_attempt(completed_a)
            assert attempt_b and attempt_b != attempt_a
            assert publications == [(str(task_id), 2)]

        thread_b = threading.Thread(
            target=run_worker,
            args=("b", attempt_b),
            name="terminal-attempt-b",
        )
        thread_b.start()
        assert await asyncio.to_thread(b_running.wait, 5)

        resume_a_terminal.set()
        await asyncio.to_thread(thread_a.join, 5)
        assert not thread_a.is_alive()

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.meta[operations.ADMIN_DISPATCH_META_KEY]["attempt"] == 2
            assert current.status == "running"
    finally:
        resume_a_terminal.set()
        finish_b.set()
        if thread_a.is_alive():
            await asyncio.to_thread(thread_a.join, 5)
        if thread_b is not None and thread_b.is_alive():
            await asyncio.to_thread(thread_b.join, 5)
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_attempt_a_post_progress_cache_write_is_ignored_after_b_rotation(
    monkeypatch,
):
    """A guarded progress tail cannot overwrite B's durable projection."""
    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.jobs import admin_operations, worker_control
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.publisher_attempts import current_publisher_attempt
    from app.services.redis_client import get_redis
    from app.services.tasks import TaskService

    redis_client = get_redis()
    task_id = uuid4()
    attempt_a = "1"
    records = []
    progress_cache_reached = threading.Event()
    resume_progress_cache = threading.Event()
    original_set_status = operations.set_operation_status
    publications = _record_admin_publications(monkeypatch)

    async def progress_then_finish(
        _db,
        _options,
        progress_callback,
        *,
        publisher_checkpoint=None,
        **_kwargs,
    ):
        assert publisher_checkpoint is not None
        await progress_callback(
            {
                "phase": "running",
                "scanned": 1,
                "total": 1,
                "existing": 0,
                "imported": 1,
                "skipped": 0,
                "failed": 0,
            }
        )
        return {
            "jobs": 1,
            "scanned": 1,
            "existing": 0,
            "imported": 1,
            "skipped": 0,
            "failed": 0,
        }

    def pausing_progress_status(job_id, status, operation_type, **kwargs):
        progress = kwargs.get("progress") or {}
        if (
            threading.current_thread().name == "progress-attempt-a"
            and status == "running"
            and progress.get("scanned") == 1
        ):
            progress_cache_reached.set()
            assert resume_progress_cache.wait(5)
        return original_set_status(job_id, status, operation_type, **kwargs)

    monkeypatch.setattr(
        "app.services.disk_import.reconcile_downloads_to_db",
        progress_then_finish,
    )
    monkeypatch.setattr(
        admin_operations,
        "set_operation_status",
        pausing_progress_status,
    )
    monkeypatch.setattr(
        worker_control.HeartbeatPublisher,
        "start",
        lambda _self: None,
    )
    monkeypatch.setattr(
        worker_control.HeartbeatPublisher,
        "stop",
        lambda _self: None,
    )
    monkeypatch.setattr(
        "app.api.admin.settings.invalidate_storage_breakdown_cache",
        lambda: None,
    )
    monkeypatch.setattr("rq.Queue", _recording_disk_queue(records))

    worker_result = {}

    def run_attempt_a():
        try:
            worker_result["value"] = asyncio.run(
                admin_operations._run_disk_import_operation(
                    str(task_id),
                    {"source": "pixiv"},
                    attempt_a,
                )
            )
        except BaseException as exc:
            worker_result["error"] = exc

    thread_a = threading.Thread(
        target=run_attempt_a,
        name="progress-attempt-a",
    )
    try:
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await _prepare_registered_admin_task(
                db,
                task_id=task_id,
                title="progress attempt A",
                options={"source": "pixiv"},
            )
            await db.commit()
        redis_client.set("library:disk-import:active", str(task_id), ex=604800)
        original_set_status(
            str(task_id),
            "enqueued",
            "admin-disk-import",
            meta={"entity": "disk-import", "source": "pixiv"},
        )

        thread_a.start()
        assert await asyncio.to_thread(progress_cache_reached.wait, 5)

        async with async_session() as retry_db:
            service = TaskService(retry_db)
            running_a = await service.get(task_id)
            assert running_a.status == "running"
            await service.update_task(running_a, status="stale")
            await retry_db.commit()
            await tasks_api._retry_admin_task(running_a, service)
            retry_db.expire_all()
            running_a = await service.get(task_id)
            attempt_b = current_publisher_attempt(running_a)
            assert attempt_b and attempt_b != attempt_a
            assert publications == [(str(task_id), 2)]

        resume_progress_cache.set()
        await asyncio.to_thread(thread_a.join, 5)
        assert not thread_a.is_alive()

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.meta[operations.ADMIN_DISPATCH_META_KEY]["attempt"] == 2
            assert current.status == "enqueued"
            assert current.progress_data["phase"] == "enqueued"
    finally:
        resume_progress_cache.set()
        if thread_a.is_alive():
            await asyncio.to_thread(thread_a.join, 5)
        _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_publication_failure_preserves_committed_attempt_for_republish(
    monkeypatch,
):
    """A transport failure defers the exact committed retry for republish."""
    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.tasks import TaskService

    task_id = uuid4()
    attempts_seen: list[int] = []
    transport_available = False

    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)

    def enqueue_probe(_task_id, attempt, **_kwargs):
        attempts_seen.append(int(attempt))
        if not transport_available:
            raise RuntimeError("retry transport unavailable")
        return SimpleNamespace(id=f"admin-{task_id}-attempt-{attempt}")

    monkeypatch.setattr(operations, "_enqueue_admin_rq", enqueue_probe)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            task = await _prepare_registered_admin_task(
                db,
                task_id=task_id,
                title="retry enqueue A",
                status="failed",
                options={"source": "pixiv"},
            )
            await db.commit()

            result = await tasks_api._retry_admin_task(task, TaskService(db))

        assert result["job_id"] == f"admin-{task_id}-attempt-2"
        assert result["publication_state"] == operations.ADMIN_DISPATCH_PENDING
        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.status == "enqueued"
            assert current.attempts == 2
            assert current.rq_job_id == f"admin-{task_id}-attempt-2"
            dispatch = current.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert dispatch["attempt"] == 2
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PENDING
            assert dispatch["publication_failures"] == 1

        transport_available = True
        assert await operations.publish_admin_operation(task_id, 2) == "published"
        assert attempts_seen == [2, 2]
        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            dispatch = current.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert current.attempts == 2
            assert dispatch["attempt"] == 2
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PUBLISHED
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_retry_publication_cannot_fail_rotated_attempt(
    monkeypatch,
):
    """Publishing attempt 2 after attempt 3 rotates is a no-op."""
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations

    task_id = uuid4()
    enqueues: list[tuple[str, int]] = []
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)

    def enqueue_probe(published_task_id, attempt, **_kwargs):
        enqueues.append((str(published_task_id), int(attempt)))
        return SimpleNamespace(id=f"admin-{published_task_id}-attempt-{attempt}")

    monkeypatch.setattr(operations, "_enqueue_admin_rq", enqueue_probe)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await _prepare_registered_admin_task(
                db,
                task_id=task_id,
                title="retry admission A",
                status="failed",
            )
            await db.commit()

        attempt_2 = await operations.prepare_admin_operation_retry(task_id)
        assert attempt_2.attempt == 2

        async with async_session() as rotate_db:
            current = await rotate_db.get(TaskRun, task_id)
            current.status = "failed"
            await rotate_db.commit()
        attempt_3 = await operations.prepare_admin_operation_retry(task_id)
        assert attempt_3.attempt == 3

        assert await operations.publish_admin_operation(task_id, 2) == "skipped"
        assert enqueues == []

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.status == "enqueued"
            assert current.attempts == 3
            assert current.rq_job_id == f"admin-{task_id}-attempt-3"
            assert current.meta[operations.ADMIN_DISPATCH_META_KEY]["attempt"] == 3
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


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


def _clear_disk_operation_redis(redis_client, *task_ids) -> None:
    keys = {"library:disk-import:active"}
    for task_id in task_ids:
        keys.update(redis_client.scan_iter(match=f"*{task_id}*"))
    if keys:
        redis_client.delete(*keys)


def _recording_disk_queue(records):
    class RecordingQueue:
        def __init__(self, name, connection):
            assert name == "maintenance"
            self.name = name
            self.connection = connection

        def enqueue(self, *args, **kwargs):
            records.append((args, kwargs))
            return SimpleNamespace(id=f"rq-disk-{len(records)}")

    return RecordingQueue


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_disk_enqueue_compensation_leaves_all_state_untouched(
    monkeypatch,
):
    """A superseded enqueue error cannot alter Redis or the current TaskRun."""
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.operations import compensate_operation_enqueue_failure
    from app.services.tasks import TaskService

    task_id = uuid4()
    attempt_a = uuid4().hex
    attempt_b = uuid4().hex
    redis_mutations: list[str] = []

    class MutationProbeRedis:
        def eval(self, *_args, **_kwargs):
            redis_mutations.append("release")
            return 1

    monkeypatch.setattr(
        operations,
        "set_operation_status",
        lambda *_args, **_kwargs: redis_mutations.append("status"),
    )

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await TaskService(db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="current publisher",
                status="enqueued",
                queue_name="maintenance",
                meta={_PUBLISHER_ATTEMPT_META_KEY: attempt_b},
            )
            await db.commit()

        await compensate_operation_enqueue_failure(
            str(task_id),
            "admin-disk-import",
            RuntimeError("late enqueue failure from attempt A"),
            lock_key="library:disk-import:active",
            redis_client=MutationProbeRedis(),
            publisher_attempt=attempt_a,
        )

        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, task_id)
            assert task.status == "enqueued"
            assert task.meta[_PUBLISHER_ATTEMPT_META_KEY] == attempt_b
            assert redis_mutations == []
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_publication_cannot_remove_retry_scope_owner(
    monkeypatch,
):
    """A stale attempt cannot publish or admit an independent operation."""
    from fastapi import HTTPException

    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations

    task_id = uuid4()
    enqueues: list[tuple[str, int]] = []
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)

    def enqueue_probe(published_task_id, attempt, **_kwargs):
        enqueues.append((str(published_task_id), int(attempt)))
        return SimpleNamespace(id=f"admin-{published_task_id}-attempt-{attempt}")

    monkeypatch.setattr(operations, "_enqueue_admin_rq", enqueue_probe)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await _prepare_registered_admin_task(
                db,
                task_id=task_id,
                title="attempt A",
                status="failed",
                options={"source": "pixiv"},
            )
            await db.commit()

        retry_b = await operations.prepare_admin_operation_retry(task_id)
        assert retry_b.attempt == 2
        assert await operations.publish_admin_operation(task_id, 1) == "skipped"
        assert enqueues == []

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.status == "enqueued"
            assert current.attempts == 2
            assert current.meta[operations.ADMIN_DISPATCH_META_KEY]["attempt"] == 2
            with pytest.raises(HTTPException) as conflict:
                await data_api.import_from_disk(
                    ImportFromDiskRequest(source="pixiv"),
                    verify_db,
                )
            assert conflict.value.status_code == 409
            assert conflict.value.detail["task_id"] == str(task_id)
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initial_queue_exception_keeps_committed_intent_recoverable(monkeypatch):
    """The initial API returns a pending durable intent on transport failure."""
    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations

    captured = {}
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)

    def reject(task_id, attempt, **_kwargs):
        captured["task_id"] = str(task_id)
        captured["attempt"] = int(attempt)
        raise RuntimeError("queue transport unavailable")

    monkeypatch.setattr(operations, "_enqueue_admin_rq", reject)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            response = await data_api.import_from_disk(
                ImportFromDiskRequest(source="pixiv"),
                db,
            )

        assert captured == {"task_id": response["task_id"], "attempt": 1}
        assert response["publication_state"] == operations.ADMIN_DISPATCH_PENDING
        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, UUID(response["task_id"]))
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.status == "enqueued"
            assert task.rq_job_id == response["job_id"]
            assert dispatch["attempt"] == 1
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PENDING
            assert dispatch["publication_failures"] == 1
            assert dispatch["last_error"] == "queue transport unavailable"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


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

            from app.services.search import SearchService

            result = await SearchService(db).search(
                "pixiv",
                scope="tasks",
                permissions={"tasks"},
            )
            tasks = result["groups"]["tasks"]
            assert tasks["total"] >= 1
            assert any(task["id"] == str(download_task.id) for task in tasks["items"])
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pause_failed_task_is_rejected_as_a_structured_conflict(monkeypatch):
    """Terminal failures are retryable, but no longer present a fake pause action."""
    from fastapi import HTTPException

    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.subscription import Subscription
    from app.services.redis_pubsub import TaskEventPublisher
    from app.services.tasks import TaskService

    monkeypatch.setattr(TaskEventPublisher, "send_control", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(TaskEventPublisher, "publish_status_change", lambda *_args, **_kwargs: None)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            creator = Creator(name="pause-failed")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="Pause failed")
            db.add(subscription)
            await db.flush()
            download = DownloadJob(
                subscription_id=subscription.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/123",
                status="failed",
            )
            db.add(download)
            await db.flush()
            task = await TaskService(db).ensure_download_task(download)
            await db.commit()

            with pytest.raises(HTTPException) as error:
                await tasks_api._control_task(task.id, "pause", db, "operator")

            assert error.value.status_code == 409
            assert error.value.detail["code"] == "invalid_task_action"
            assert error.value.detail["action"] == "pause"
            await db.refresh(download)
            assert download.status == "failed"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_admin_retry_state_has_structured_conflict_detail():
    from fastapi import HTTPException

    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)
            task = await _prepare_registered_admin_task(db)
            await db.commit()

            with pytest.raises(HTTPException) as error:
                await tasks_api._retry_admin_task(task, svc)

            assert error.value.status_code == 409
            assert error.value.detail == {
                "code": "invalid_task_action",
                "action": "retry",
                "status": "enqueued",
                "message": "Task is enqueued; retry is only available after failure",
            }
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_admin_disk_import_task_requeues_same_task(monkeypatch):
    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.tasks import TaskService

    publications = _record_admin_publications(monkeypatch)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)
            task = await _prepare_registered_admin_task(
                db,
                status="failed",
                options={"source": "pixiv"},
            )
            await db.commit()

            result = await tasks_api._retry_admin_task(task, svc)

            assert result == {
                "task_id": str(task.id),
                "job_id": f"admin-{task.id}-attempt-2",
                "status": "enqueued",
                "operation_type": "admin-disk-import",
                "publication_state": operations.ADMIN_DISPATCH_PUBLISHED,
            }
            assert publications == [(str(task.id), 2)]

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task.id)
            dispatch = current.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert current.status == "enqueued"
            assert current.finished_at is None
            assert current.error_log == ""
            assert current.rq_job_id == f"admin-{task.id}-attempt-2"
            assert current.attempts == 2
            assert dispatch["attempt"] == 2
            assert dispatch["options"] == {"source": "pixiv"}
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_initial_disk_import_persists_attempt_before_rq_publication(
    monkeypatch,
):
    """Initial publication uses the exact committed registry attempt."""
    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations

    queued = []
    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_a, **_k: None)

    def record_enqueue(task_id, attempt, **kwargs):
        queued.append((str(task_id), int(attempt), kwargs))
        return SimpleNamespace(id=kwargs["rq_job_id"])

    monkeypatch.setattr(operations, "_enqueue_admin_rq", record_enqueue)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            response = await data_api.import_from_disk(
                ImportFromDiskRequest(source="pixiv"),
                db,
            )

            assert len(queued) == 1
            queued_task_id, attempt, kwargs = queued[0]
            assert queued_task_id == response["task_id"]
            assert attempt == 1
            assert kwargs["operation_type"] == "admin-disk-import"
            assert kwargs["rq_job_id"] == response["job_id"]
            assert kwargs["queue_name"] == "maintenance"
            assert kwargs["job_timeout"] == 14400

        async with async_session() as verify_db:
            task = await verify_db.get(TaskRun, UUID(response["task_id"]))
            dispatch = task.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert task.attempts == 1
            assert task.rq_job_id == response["job_id"]
            assert dispatch["attempt"] == 1
            assert dispatch["rq_job_id"] == response["job_id"]
            assert dispatch["options"] == {
                "source": "pixiv",
                "repository_id": None,
                "reset_ledger": False,
            }
            assert dispatch["publication_state"] == operations.ADMIN_DISPATCH_PUBLISHED
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_initial_retry_and_recovery_rq_descriptions_are_deterministic(
    monkeypatch,
    caplog,
):
    """Real RQ records/logs identify stable tasks and exact attempts."""
    from rq import Queue, SimpleWorker
    from rq.job import Job

    from app.api import tasks as tasks_api
    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine
    from app.jobs import admin_operations, import_runner
    from app.models.task_run import TaskRun
    from app.services.import_dispatch import _enqueue_import_rq
    from app.services.publisher_attempts import current_publisher_attempt
    from app.services.redis_client import get_redis
    from app.services.tasks import TaskService

    redis_client = get_redis()
    maintenance = Queue(name="maintenance", connection=redis_client)
    imports = Queue(name="imports", connection=redis_client)
    queued_jobs = []
    task_id = None

    try:
        maintenance.empty()
        imports.empty()
        _clear_disk_operation_redis(redis_client)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            response = await data_api.import_from_disk(
                ImportFromDiskRequest(source="pixiv"),
                db,
            )
            task_id = UUID(response["task_id"])
            initial_task = await db.get(TaskRun, task_id)
            attempt_a = current_publisher_attempt(initial_task)
            initial_job = Job.fetch(initial_task.rq_job_id, connection=redis_client)
            queued_jobs.append(initial_job)

            service = TaskService(db)
            await service.update_task(initial_task, status="failed")
            await db.commit()
            await tasks_api._retry_admin_task(initial_task, service)
            db.expire_all()
            initial_task = await db.get(TaskRun, task_id)
            attempt_b = current_publisher_attempt(initial_task)
            retry_job = Job.fetch(initial_task.rq_job_id, connection=redis_client)
            queued_jobs.append(retry_job)

        recovery_import_id = uuid4()
        recovery_rq_id = f"import-{recovery_import_id}-attempt-1"
        recovery_job = _enqueue_import_rq(
            recovery_import_id,
            recovery_rq_id,
            3600,
            redis_client=redis_client,
        )
        queued_jobs.append(recovery_job)

        initial_description = f"admin task={task_id} attempt=1"
        retry_description = f"admin task={task_id} attempt=2"
        assert initial_job.description == initial_description
        assert retry_job.description == retry_description
        assert initial_job.args == (str(task_id), 1)
        assert retry_job.args == (str(task_id), 2)
        assert recovery_job.description == f"import task={recovery_import_id}"

        monkeypatch.setattr(
            admin_operations,
            "run_registered_admin_operation",
            lambda job_id, attempt: {
                "job_id": job_id,
                "attempt": attempt,
            },
        )
        monkeypatch.setattr(
            import_runner,
            "run_import_job",
            lambda import_job_id: {"import_job_id": import_job_id},
        )
        caplog.set_level(logging.INFO)
        worker = SimpleWorker(
            [maintenance, imports],
            connection=redis_client,
            name=f"privacy-worker-{uuid4().hex[:8]}",
        )
        assert worker.work(burst=True, max_jobs=3, logging_level="INFO") is True

        assert initial_description in caplog.text
        assert retry_description in caplog.text
        assert f"import task={recovery_import_id}" in caplog.text
        assert attempt_a == "1"
        assert attempt_b == "2"
    finally:
        maintenance.empty()
        imports.empty()
        for job in queued_jobs:
            try:
                job.delete()
            except Exception:
                pass
        if task_id is not None:
            _clear_disk_operation_redis(redis_client, task_id)
        else:
            _clear_disk_operation_redis(redis_client)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_publisher_success_and_failure_public_projections_are_consistent(
    monkeypatch,
):
    """Registered terminal outcomes agree across task, API, cache, and events."""
    from app.api.admin import data as data_api
    from app.api.admin.data import ImportFromDiskRequest
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations, redis_pubsub
    from app.services.operations import compensate_operation_enqueue_failure
    from app.services.publisher_attempts import current_publisher_attempt
    from app.services.redis_client import get_redis
    from app.services.redis_pubsub import TaskEventPublisher
    from app.services.tasks import TaskService, task_payload

    redis_client = get_redis()
    records = []
    task_ids = []
    published = []

    class EventRedis:
        def publish(self, channel, payload):
            published.append((channel, json.loads(payload)))
            return 1

    monkeypatch.setattr("rq.Queue", _recording_disk_queue(records))
    monkeypatch.setattr(redis_pubsub, "get_redis", lambda: EventRedis())

    try:
        _clear_disk_operation_redis(redis_client)
        async with async_session() as db:
            await _clear_task_test_tables(db)
            failed_response = await data_api.import_from_disk(
                ImportFromDiskRequest(source="pixiv"),
                db,
            )
            failed_id = UUID(failed_response["task_id"])
            task_ids.append(failed_id)
            failed_task = await db.get(TaskRun, failed_id)
            failed_attempt = current_publisher_attempt(failed_task)

        await compensate_operation_enqueue_failure(
            str(failed_id),
            "admin-disk-import",
            RuntimeError(
                f"queue rejected private publisher attempt {failed_attempt}"
            ),
            lock_key="library:disk-import:active",
            redis_client=redis_client,
            publisher_attempt=failed_attempt,
        )

        # A second task covers the successful terminal projection independently.
        _clear_disk_operation_redis(redis_client)
        async with async_session() as db:
            successful_response = await data_api.import_from_disk(
                ImportFromDiskRequest(source="pixiv"),
                db,
            )
            successful_id = UUID(successful_response["task_id"])
            task_ids.append(successful_id)
            successful_task = await db.get(TaskRun, successful_id)
            successful_attempt = current_publisher_attempt(successful_task)
            await TaskService(db).update_task(
                successful_task,
                status="complete",
                progress={"phase": "complete", "label": "Import complete"},
                result={"jobs": 1},
            )
            await db.commit()
        operations.set_operation_status(
            str(successful_id),
            "complete",
            "admin-disk-import",
            progress={"phase": "complete", "label": "Import complete"},
            result={"jobs": 1},
            meta={"entity": "disk-import", "source": "pixiv"},
        )

        TaskEventPublisher.publish_status_change(
            str(successful_id),
            "admin",
            "running",
            "complete",
        )

        async with async_session() as verify_db:
            failed_task = await verify_db.get(TaskRun, failed_id)
            successful_task = await verify_db.get(TaskRun, successful_id)
            failed_events = await TaskService(verify_db).task_events(failed_id)
            successful_events = await TaskService(verify_db).task_events(
                successful_id
            )
            public_values = {
                "failed_response": failed_response,
                "failed_detail": task_payload(failed_task, failed_events),
                "failed_cache": operations.get_operation_status(str(failed_id)),
                "failed_operation_api": await data_api.get_admin_operation(
                    str(failed_id)
                ),
                "successful_response": successful_response,
                "successful_detail": task_payload(
                    successful_task,
                    successful_events,
                ),
                "successful_cache": operations.get_operation_status(
                    str(successful_id)
                ),
                "successful_operation_api": await data_api.get_admin_operation(
                    str(successful_id)
                ),
                "events": published,
            }

        serialized = json.dumps(public_values, default=str)
        assert failed_attempt == "1"
        assert successful_attempt == "1"
        assert failed_task.status == "failed"
        assert successful_task.status == "complete"
        assert successful_task.result_data == {"jobs": 1}
        assert "[redacted publisher attempt]" in serialized
        assert _PUBLISHER_ATTEMPT_META_KEY not in serialized
        assert published[-1][1]["new_status"] == "complete"
    finally:
        for task_id in task_ids:
            _clear_disk_operation_redis(redis_client, task_id)
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_admin_gitllery_sync_task_requeues_with_options(monkeypatch):
    from app.api import tasks as tasks_api
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services import operations
    from app.services.tasks import TaskService

    publications = _record_admin_publications(monkeypatch)

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)
            task = await _prepare_registered_admin_task(
                db,
                operation_type="admin-gitllery-sync",
                scope_key="library:gitllery-sync:active",
                title="Gitllery sync",
                entity="gitllery-sync",
                status="failed",
                options={"mode": "reconcile", "repository_id": None},
            )
            await db.commit()

            result = await tasks_api._retry_admin_task(task, svc)

            assert result == {
                "task_id": str(task.id),
                "job_id": f"admin-{task.id}-attempt-2",
                "status": "enqueued",
                "operation_type": "admin-gitllery-sync",
                "publication_state": operations.ADMIN_DISPATCH_PUBLISHED,
            }
            assert publications == [(str(task.id), 2)]

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task.id)
            dispatch = current.meta[operations.ADMIN_DISPATCH_META_KEY]
            assert current.status == "enqueued"
            assert current.attempts == 2
            assert dispatch["attempt"] == 2
            assert dispatch["options"] == {
                "mode": "reconcile",
                "repository_id": None,
            }
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()
