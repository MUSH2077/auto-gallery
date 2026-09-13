import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import redis
import pytest
from rq import Worker
from sqlalchemy import text

from app.services.resource_aware_worker import (
    ResourceAwareWorker,
    adaptive_wait_delay,
    redis_retry_delay,
)


class _Connection:
    def __init__(self):
        self.fields = {}
        self.hset_calls = 0

    def hset(self, key, mapping):
        self.hset_calls += 1
        self.fields.update(mapping)

    def exists(self, *_keys):
        return 0


class _Log:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def test_light_worker_dequeues_during_critical_pressure(monkeypatch):
    worker = object.__new__(ResourceAwareWorker)
    worker.connection = _Connection()
    worker.name = "test-worker"
    worker.queues = [type("Queue", (), {"name": "scheduled"})()]
    worker.log = _Log()
    worker.set_state = lambda *_args, **_kwargs: None
    worker.procline = lambda *_args, **_kwargs: None
    worker.heartbeat = lambda *_args, **_kwargs: None
    worker.run_maintenance_tasks = lambda: None

    monkeypatch.setattr(
        "app.services.resource_aware_worker.get_resource_pressure_snapshot_sync",
        lambda redis_client=None: (_ for _ in ()).throw(
            AssertionError("light queues must not poll the heavy pressure gate")
        ),
    )
    monkeypatch.setattr("app.services.resource_aware_worker.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(ResourceAwareWorker, "should_run_maintenance_tasks", False)
    monkeypatch.setattr(
        Worker,
        "dequeue_job_and_maintain_ttl",
        lambda self, timeout, max_idle_time=None: (timeout, max_idle_time),
    )

    result = worker.dequeue_job_and_maintain_ttl(420, None)

    assert result == (420, None)
    assert worker.connection.fields == {}


def _bare_worker():
    worker = object.__new__(ResourceAwareWorker)
    worker.connection = _Connection()
    worker.name = "test-worker"
    worker.log = _Log()
    worker.set_state = lambda *_args, **_kwargs: None
    worker.procline = lambda *_args, **_kwargs: None
    worker.run_maintenance_tasks = lambda: None
    worker.last_cleaned_at = datetime.now(timezone.utc)
    worker.maintenance_interval = 600
    return worker


def test_snapshot_redis_timeout_stays_fail_closed_and_retries(monkeypatch):
    snapshots = iter(
        [
            redis.exceptions.TimeoutError("redis timeout"),
            {"status": "paused", "reasons": ["swap_free_critical"]},
            {"status": "normal", "reasons": []},
        ]
    )
    sleeps = []
    worker = _bare_worker()
    worker.heartbeat = lambda *_args, **_kwargs: None

    def pressure_snapshot(redis_client=None):
        value = next(snapshots)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        "app.services.resource_aware_worker.get_resource_pressure_snapshot_sync",
        pressure_snapshot,
    )
    monkeypatch.setattr("app.services.resource_aware_worker.time.sleep", sleeps.append)
    monkeypatch.setattr(ResourceAwareWorker, "should_run_maintenance_tasks", False)
    result = worker._wait_until_pressure_allows_dequeue(10, workload="download")

    assert result["status"] == "normal"
    assert sleeps[0] == 1.0
    assert 3.4 <= sleeps[1] <= 4.6


def test_paused_heartbeat_connection_error_does_not_escape(monkeypatch):
    snapshots = iter(
        [
            {"status": "paused", "reasons": ["io_psi_critical"]},
            {"status": "paused", "reasons": ["io_psi_critical"]},
            {"status": "normal", "reasons": []},
        ]
    )
    sleeps = []
    heartbeat_calls = {"count": 0}
    worker = _bare_worker()

    def heartbeat(*_args, **_kwargs):
        heartbeat_calls["count"] += 1
        if heartbeat_calls["count"] == 1:
            raise redis.exceptions.ConnectionError("redis unavailable")

    worker.heartbeat = heartbeat
    monkeypatch.setattr(
        "app.services.resource_aware_worker.get_resource_pressure_snapshot_sync",
        lambda redis_client=None: next(snapshots),
    )
    monkeypatch.setattr("app.services.resource_aware_worker.time.sleep", sleeps.append)
    monkeypatch.setattr(ResourceAwareWorker, "should_run_maintenance_tasks", False)
    assert worker._wait_until_pressure_allows_dequeue(10, workload="import")["status"] == "normal"
    assert heartbeat_calls["count"] == 2
    assert sleeps[0] == 1.0
    assert 3.4 <= sleeps[1] <= 4.6


def test_redis_retry_delay_is_capped():
    assert [redis_retry_delay(index) for index in range(1, 8)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        15.0,
        30.0,
        30.0,
    ]


def test_adaptive_wait_is_jittered_and_capped():
    assert adaptive_wait_delay(0, jitter=False) == 2.0
    assert adaptive_wait_delay(3, jitter=False) == 16.0
    assert adaptive_wait_delay(20, jitter=False) == 30.0
    assert adaptive_wait_delay(10**6, jitter=False) == 30.0


def test_worker_pressure_hash_writes_only_on_change_or_heartbeat(monkeypatch):
    worker = _bare_worker()
    now = {"value": 10.0}
    monkeypatch.setattr("app.services.resource_aware_worker.time.monotonic", lambda: now["value"])
    snapshot = {
        "status": "warning",
        "controller_mode": "constrained",
        "reasons": ["io_psi_high"],
        "budget": {"generation": 3, "throughput_scale": 0.25},
    }

    worker._publish_pressure_state(snapshot)
    worker._publish_pressure_state(snapshot)
    assert worker.connection.hset_calls == 1

    now["value"] += 30
    worker._publish_pressure_state(snapshot)
    assert worker.connection.hset_calls == 2


def test_worker_cgroup_oom_kill_is_immediately_fail_closed_and_promoted(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    samples = iter(
        [
            {"max": 0, "oom": 0, "oom_kill": 0},
            {"max": 0, "oom": 1, "oom_kill": 1},
        ]
    )
    promoted = []
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": next(samples),
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "worker-test",
        },
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "publish_external_resource_critical",
        lambda reason, **kwargs: (
            promoted.append((reason, kwargs["source"]))
            or {
                "status": "paused",
                "controller_mode": "critical",
                "reasons": [reason],
                "budget": {"throughput_scale": 0.0},
            }
        ),
    )

    normal = {"status": "normal", "controller_mode": "normal", "reasons": []}
    assert worker._publish_pressure_state(dict(normal))["hard_gate_active"] is False
    snapshot = dict(normal)
    feedback = worker._publish_pressure_state(snapshot)

    assert feedback["hard_gate_active"] is True
    assert feedback["cgroup_deltas"] == {"max": 0, "oom": 1, "oom_kill": 1}
    assert snapshot["status"] == "paused"
    assert promoted == [("worker_cgroup_oom_kill", "test-worker")]


def test_worker_retries_unacknowledged_oom_event_with_the_same_identity(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    contributions = iter(
        [
            {"max": 0, "oom": 1, "oom_kill": 1},
            {"max": 0, "oom": 1, "oom_kill": 1},
        ]
    )
    calls = []
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": next(contributions),
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "/docker/shared",
        },
    )

    def promote(reason, **kwargs):
        calls.append((reason, kwargs))
        if len(calls) == 1:
            raise RuntimeError("redis unavailable")
        return {
            "status": "paused",
            "controller_mode": "critical",
            "reasons": [reason],
            "budget": {"throughput_scale": 0.0},
        }

    monkeypatch.setattr(resource_aware_worker, "publish_external_resource_critical", promote)
    normal = {"status": "normal", "controller_mode": "normal", "reasons": []}

    first = worker._publish_pressure_state(dict(normal))
    second_snapshot = dict(normal)
    second = worker._publish_pressure_state(second_snapshot)

    assert first["hard_gate_active"] is True
    assert second["hard_gate_active"] is True
    assert second_snapshot["status"] == "paused"
    assert len(calls) == 2
    assert calls[0][1]["event_id"] == calls[1][1]["event_id"]
    assert calls[0][1]["cgroup_id"] == "/docker/shared"
    assert calls[0][1]["oom_kill_counter"] == 1


def test_worker_keeps_local_gate_closed_past_recovery_while_oom_is_unacknowledged(
    monkeypatch,
):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    now = {"value": 10.0}
    monkeypatch.setattr(resource_aware_worker.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": {"max": 0, "oom": 1, "oom_kill": 1},
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "/docker/unacknowledged",
        },
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "publish_external_resource_critical",
        lambda _reason, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("promotion unavailable")
        ),
    )
    normal = {"status": "normal", "controller_mode": "normal", "reasons": []}

    first = worker._publish_pressure_state(dict(normal))
    now["value"] += 61.0
    second = worker._publish_pressure_state(dict(normal))

    assert first["hard_gate_active"] is True
    assert second["hard_gate_active"] is True
    assert worker._pending_cgroup_oom_event is not None


def test_worker_local_recovery_requires_confirmed_oom_acknowledgment(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    worker._last_cgroup_events = {"max": 0, "oom": 0, "oom_kill": 0}
    worker._local_cgroup_oom_kill_latched = True
    worker._local_cgroup_oom_kill_at = 10.0
    worker._local_cgroup_oom_acknowledged = False
    worker._pending_cgroup_oom_event = None
    monkeypatch.setattr(resource_aware_worker.time, "monotonic", lambda: 71.0)
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": {"max": 0, "oom": 0, "oom_kill": 0},
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "/docker/unconfirmed",
        },
    )

    feedback = worker._publish_pressure_state(
        {"status": "normal", "controller_mode": "normal", "reasons": []}
    )

    assert feedback["hard_gate_active"] is True


def test_restarted_worker_does_not_latch_an_acknowledged_historical_oom(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": {"max": 0, "oom": 1, "oom_kill": 4},
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "/docker/shared",
        },
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "publish_external_resource_critical",
        lambda _reason, **_kwargs: None,
    )

    feedback = worker._publish_pressure_state(
        {"status": "normal", "controller_mode": "normal", "reasons": []}
    )

    assert feedback["hard_gate_active"] is False


def test_live_worker_periodically_touches_recovered_cgroup_ack(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    now = {"value": 0.0}
    touches = []
    monkeypatch.setattr(resource_aware_worker.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": {"max": 0, "oom": 1, "oom_kill": 4},
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "/docker/long-lived",
        },
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "publish_external_resource_critical",
        lambda _reason, **_kwargs: None,
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "touch_cgroup_oom_kill_ack",
        lambda connection, cgroup_id, counter: (
            touches.append((connection, cgroup_id, counter)) or "recovered"
        ),
        raising=False,
    )
    normal = {"status": "normal", "controller_mode": "normal", "reasons": []}

    worker._publish_pressure_state(dict(normal))
    now["value"] = 60 * 60 + 1
    feedback = worker._publish_pressure_state(dict(normal))

    assert touches == [(worker.connection, "/docker/long-lived", 4)]
    assert feedback["hard_gate_active"] is False


def test_worker_cgroup_max_and_oom_only_apply_local_soft_feedback(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    now = {"value": 10.0}
    samples = iter(
        [
            {"max": 0, "oom": 0, "oom_kill": 0},
            {"max": 1, "oom": 1, "oom_kill": 0},
        ]
    )
    promoted = []
    monkeypatch.setattr(resource_aware_worker.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        resource_aware_worker,
        "sample_cgroup_contribution",
        lambda: {
            "memory_events": next(samples),
            "memory": {},
            "cpu": {},
            "io": {},
            "psi": {},
            "cgroup_id": "worker-test",
        },
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "publish_external_resource_critical",
        lambda reason, **kwargs: promoted.append((reason, kwargs["source"])),
    )

    shared_snapshot = {"status": "normal", "controller_mode": "normal", "reasons": []}
    assert worker._publish_pressure_state(shared_snapshot)["soft_scale"] == 1.0
    feedback = worker._publish_pressure_state(shared_snapshot)

    assert feedback == {
        "hard_gate_active": False,
        "soft_scale": 0.5,
        "cgroup_deltas": {"max": 1, "oom": 1, "oom_kill": 0},
    }
    assert shared_snapshot == {"status": "normal", "controller_mode": "normal", "reasons": []}
    assert promoted == []
    assert worker._local_cgroup_soft_until == 70.0


def test_worker_soft_cgroup_feedback_halves_an_enforced_slice():
    worker = _bare_worker()
    worker._local_cgroup_soft_until = float("inf")
    job = type(
        "Job",
        (),
        {
            "func_name": "app.jobs.import_projection.run_import_projection_outbox",
            "args": (25, 20.0),
        },
    )()
    snapshot = {
        "budget": {
            "governance_mode": "enforce",
            "profiles": {
                "import_db": {"work_units": 10, "slice_seconds": 8.0},
            },
        },
    }

    worker._apply_profile_slice(job, "import_db", snapshot)

    assert job.args == (5, 4.0)


def test_worker_rejects_mixed_heavy_and_non_heavy_queues():
    worker = _bare_worker()
    worker.queues = [
        type("Queue", (), {"name": "downloads"})(),
        type("Queue", (), {"name": "scheduled"})(),
    ]

    with pytest.raises(RuntimeError, match="cannot mix heavy and non-heavy"):
        worker._uses_heavy_gate()


def test_heavy_worker_dequeues_without_taking_global_flock(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    queue = type("Queue", (), {"name": "downloads", "key": "rq:queue:downloads"})()
    worker.queues = [queue]
    worker.heartbeat = lambda *_args, **_kwargs: None
    base_calls = []
    job = type("Job", (), {"id": "job-1"})()

    worker.connection.exists = lambda *_keys: 1
    monkeypatch.setattr(ResourceAwareWorker, "should_run_maintenance_tasks", False)
    monkeypatch.setattr(
        resource_aware_worker,
        "get_resource_pressure_snapshot_sync",
        lambda redis_client=None: {"status": "normal", "reasons": []},
    )
    monkeypatch.setattr(
        Worker,
        "dequeue_job_and_maintain_ttl",
        lambda self, timeout, max_idle_time=None: (
            base_calls.append((timeout, max_idle_time)) or (job, queue)
        ),
    )

    assert worker.dequeue_job_and_maintain_ttl(420, None) == (job, queue)
    assert base_calls == [(1, 1)]


def test_operations_worker_selects_profile_from_job_function(monkeypatch):
    from app.services import heavy_io, resource_aware_worker

    worker = _bare_worker()
    worker.heartbeat = lambda *_args, **_kwargs: None
    job = type("Job", (), {
        "id": "job-2",
        "func_name": "app.jobs.search_projection.run_search_projection_outbox",
        "meta": {},
        "args": (),
    })()
    queue = type("Queue", (), {"name": "imports", "key": "rq:queue:imports"})()
    worker.queues = [queue]
    events = []

    class AcquiredLock:
        def release(self):
            events.append("released")

    def execute_workhorse(_self, actual_job, actual_queue):
        assert (actual_job, actual_queue) == (job, queue)
        assert heavy_io.worker_flock_is_inherited() is True
        assert heavy_io.worker_inherited_profile() == "search_index"
        events.append("workhorse")

    worker._profile_admission = lambda actual_job, workload: (
        events.append(workload) or (AcquiredLock(), None, str(actual_job.id))
    )
    monkeypatch.setattr(Worker, "execute_job", execute_workhorse)

    worker.execute_job(job, queue)

    assert events == ["search_index", "workhorse", "released"]
    assert heavy_io.worker_flock_is_inherited() is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disk_publisher_parent_admission_cannot_mutate_rotated_attempt_resource_state():
    """The pre-workhorse admission projection belongs to the captured attempt."""
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services.heavy_io import register_resource_state_callback
    from app.services.tasks import TaskService, update_task_resource_state

    task_id = uuid4()
    attempt_a = uuid4().hex
    attempt_b = uuid4().hex
    worker = _bare_worker()
    worker._wait_until_pressure_allows_dequeue = lambda *_args, **_kwargs: {}
    job = type(
        "Job",
        (),
        {
            "id": "rq-stale-admission",
            "func_name": "app.jobs.admin_operations.run_disk_import_operation",
            "args": (str(task_id), {}, attempt_a),
            "meta": {},
            "save_meta": lambda self: None,
        },
    )()

    try:
        register_resource_state_callback(update_task_resource_state)
        async with async_session() as db:
            await db.execute(text("TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE"))
            await TaskService(db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="rotated admission",
                status="enqueued",
                queue_name="maintenance",
                meta={"_bounded_import_publisher_attempt": attempt_b},
            )
            await db.commit()

        result = await asyncio.to_thread(
            worker._profile_admission,
            job,
            "maintenance",
        )
        assert result == (None, None, str(task_id))

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.meta["_bounded_import_publisher_attempt"] == attempt_b
            assert current.resource_state == "waiting"
            assert current.resource_reason is None
    finally:
        register_resource_state_callback(None)
        async with async_session() as db:
            await db.execute(text("TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE"))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legacy_disk_parent_admission_cannot_mutate_attempt_owned_resource_state():
    """A no-token rolling RQ delivery has no authority over attempt B."""
    from app.database import async_session, engine
    from app.models.task_run import TaskRun
    from app.services.heavy_io import register_resource_state_callback
    from app.services.tasks import TaskService, update_task_resource_state

    task_id = uuid4()
    attempt_b = uuid4().hex
    worker = _bare_worker()
    worker._wait_until_pressure_allows_dequeue = lambda *_args, **_kwargs: {}
    job = type(
        "Job",
        (),
        {
            "id": "rq-legacy-admission",
            "func_name": "app.jobs.admin_operations.run_disk_import_operation",
            "args": (str(task_id), {}),
            "meta": {},
            "save_meta": lambda self: None,
        },
    )()

    try:
        register_resource_state_callback(update_task_resource_state)
        async with async_session() as db:
            await db.execute(text("TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE"))
            await TaskService(db).create_task(
                task_id=task_id,
                kind="admin",
                operation_type="admin-disk-import",
                title="attempt-owned legacy admission",
                status="enqueued",
                queue_name="maintenance",
                resource_state="waiting",
                meta={"_bounded_import_publisher_attempt": attempt_b},
            )
            await db.commit()

        result = await asyncio.to_thread(
            worker._profile_admission,
            job,
            "maintenance",
        )
        assert result == (None, None, str(task_id))

        async with async_session() as verify_db:
            current = await verify_db.get(TaskRun, task_id)
            assert current.meta["_bounded_import_publisher_attempt"] == attempt_b
            assert current.resource_state == "waiting"
            assert current.resource_reason is None
    finally:
        register_resource_state_callback(None)
        async with async_session() as db:
            await db.execute(text("TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE"))
            await db.commit()
        await engine.dispose()


@pytest.mark.parametrize(
    ("func_name", "expected"),
    [
        ("app.jobs.media_derivatives.run_media_derivative_outbox", "image_derive"),
        ("app.jobs.search_projection.run_search_projection_outbox", "search_index"),
        ("app.jobs.gitllery_projection.run_gitllery_projection_outbox", "git_projection"),
        ("app.jobs.import_projection.run_import_projection_outbox", "import_db"),
        ("app.jobs.admin_operations.run_gitllery_sync_operation", "git_projection"),
    ],
)
def test_operations_outbox_profile_classification(func_name, expected):
    job = type("Job", (), {"func_name": func_name, "meta": {}})()
    queue = type("Queue", (), {"name": "operations"})()

    assert ResourceAwareWorker._job_workload(job, queue) == expected


def test_asset_dedup_coordinator_uses_child_owned_image_slices():
    job = type(
        "Job",
        (),
        {"func_name": "app.jobs.asset_dedup.run_asset_dedup_outbox", "meta": {}},
    )()
    queue = type("Queue", (), {"name": "operations"})()

    assert ResourceAwareWorker._job_workload(job, queue) == "image_derive"
    assert ResourceAwareWorker._internal_slice_workload(job) == "image_derive"


def test_registered_admin_transport_preserves_child_owned_resource_profile():
    job = type(
        "Job",
        (),
        {
            "func_name": "app.jobs.admin_operations.run_registered_admin_operation",
            "meta": {
                "registered_admin_operation": "asset-dedup-scan",
                "registered_admin_internal_profile": "image_derive",
            },
        },
    )()
    queue = type("Queue", (), {"name": "maintenance"})()

    assert ResourceAwareWorker._job_workload(job, queue) == "image_derive"
    assert ResourceAwareWorker._internal_slice_workload(job) == "image_derive"
    assert ResourceAwareWorker._job_uses_nonblocking_child_admission(job) is True


@pytest.mark.parametrize(
    "operation_type",
    [
        "admin-integrity-scan",
        "admin-backup-estimate",
        "admin-backup-create",
        "admin-restore-validate",
        "admin-proxy-test",
        "admin-gallerydl-connectivity-test",
    ],
)
def test_unsliced_registered_admin_jobs_ignore_stale_internal_profile(operation_type):
    """Rolling queued metadata cannot suppress whole-operation admission."""
    job = type(
        "Job",
        (),
        {
            "func_name": "app.jobs.admin_operations.run_registered_admin_operation",
            "meta": {
                "registered_admin_operation": operation_type,
                "registered_admin_internal_profile": "maintenance",
            },
        },
    )()
    queue = type("Queue", (), {"name": "maintenance"})()

    assert ResourceAwareWorker._internal_slice_workload(job) is None
    assert ResourceAwareWorker._job_workload(job, queue) == "maintenance"


def test_new_unsliced_admin_deliveries_do_not_publish_internal_slice_metadata(monkeypatch):
    import rq
    from app.services import operations

    published = []
    monkeypatch.setattr(rq, "Queue", lambda name, connection: (name, connection))
    monkeypatch.setattr(
        operations,
        "checked_enqueue",
        lambda queue, function, *args, **kwargs: published.append((queue, function, args, kwargs)),
    )

    operation_types = (
        "admin-integrity-scan",
        "admin-backup-estimate",
        "admin-backup-create",
        "admin-restore-validate",
        "admin-proxy-test",
        "admin-gallerydl-connectivity-test",
    )
    for operation_type in operation_types:
        operations._enqueue_admin_rq(
            "11111111-1111-4111-8111-111111111111",
            1,
            operation_type=operation_type,
            rq_job_id=f"{operation_type}-rq",
            queue_name="maintenance",
            job_timeout=60,
            redis_client=object(),
        )
    operations._enqueue_admin_rq(
        "22222222-2222-4222-8222-222222222222",
        1,
        operation_type="admin-rebuild",
        rq_job_id="admin-rebuild-rq",
        queue_name="maintenance",
        job_timeout=60,
        redis_client=object(),
    )

    assert [entry[3]["meta"] for entry in published[:-1]] == [
        {"registered_admin_operation": operation_type}
        for operation_type in operation_types
    ]
    assert published[-1][3]["meta"] == {
        "registered_admin_operation": "admin-rebuild",
        "registered_admin_internal_profile": "maintenance",
    }


@pytest.mark.parametrize("raises", [False, True])
def test_unsliced_registered_admin_job_holds_parent_lock_through_workhorse(monkeypatch, raises):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    worker._wait_until_pressure_allows_dequeue = lambda *_args, **_kwargs: {}
    worker._raise_if_shutdown_requested = lambda: None
    worker._close_control_pubsub = lambda: None
    worker._apply_profile_slice = lambda *_args: None
    worker._set_job_resource_meta = lambda *_args: None
    job = type(
        "Job",
        (),
        {
            "id": "unsliced-admin-job",
            "func_name": "app.jobs.admin_operations.run_registered_admin_operation",
            "args": ("11111111-1111-4111-8111-111111111111", 1),
            "meta": {
                "registered_admin_operation": "admin-backup-create",
                "registered_admin_internal_profile": "maintenance",
            },
            "refresh": lambda self: None,
        },
    )()
    queue = type("Queue", (), {"name": "maintenance"})()
    held = {"value": False}

    class Lock:
        def try_acquire(self):
            assert held["value"] is False
            held["value"] = True
            return True

        def release(self):
            assert held["value"] is True
            held["value"] = False

    monkeypatch.setattr(resource_aware_worker, "resource_lease_keys", lambda _workload: [])
    monkeypatch.setattr(resource_aware_worker, "local_lock_for_workload", lambda _workload: Lock())
    monkeypatch.setattr(resource_aware_worker, "_set_resource_state_sync", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(resource_aware_worker, "profile_slice_cooldown_seconds", lambda *_args, **_kwargs: 0.0)

    def _execute(_self, actual_job, actual_queue):
        assert (actual_job, actual_queue) == (job, queue)
        assert held["value"] is True
        if raises:
            raise RuntimeError("handler failed")
        return "handled"

    monkeypatch.setattr(Worker, "execute_job", _execute)
    if raises:
        with pytest.raises(RuntimeError, match="handler failed"):
            worker.execute_job(job, queue)
    else:
        assert worker.execute_job(job, queue) == "handled"
    assert held["value"] is False


def test_outbox_slice_bounds_are_applied_before_fork():
    media = type("Job", (), {
        "func_name": "app.jobs.media_derivatives.run_media_derivative_outbox",
        "args": (25, 90.0),
    })()
    search = type("Job", (), {
        "func_name": "app.jobs.search_projection.run_search_projection_outbox",
        "args": (5000,),
    })()
    git = type("Job", (), {
        "func_name": "app.jobs.gitllery_projection.run_gitllery_projection_outbox",
        "args": (25, 90.0),
    })()

    ResourceAwareWorker._bound_job_slice(media, "video_derive")
    ResourceAwareWorker._bound_job_slice(search, "search_index")
    ResourceAwareWorker._bound_job_slice(git, "git_projection")

    assert media.args == (1, 20.0)
    assert search.args == (500,)
    assert git.args == (1, 20.0)


def test_pressure_pauses_before_flock_or_dequeue(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    queue = type("Queue", (), {"name": "operations", "key": "rq:queue:operations"})()
    worker.queues = [queue]
    worker.heartbeat = lambda *_args, **_kwargs: None
    base_calls = []

    class StopAudit(Exception):
        pass

    monkeypatch.setattr(
        resource_aware_worker,
        "get_resource_pressure_snapshot_sync",
        lambda redis_client=None: {"status": "paused", "reasons": ["memory_available_low"]},
    )
    monkeypatch.setattr(resource_aware_worker.time, "sleep", lambda _seconds: (_ for _ in ()).throw(StopAudit()))
    monkeypatch.setattr(Worker, "dequeue_job_and_maintain_ttl", lambda *args, **kwargs: base_calls.append(args))

    with pytest.raises(StopAudit):
        worker.dequeue_job_and_maintain_ttl(420, None)

    assert base_calls == []


def test_running_workhorse_heartbeat_resamples_worker_cgroup(monkeypatch):
    from app.services import resource_aware_worker

    worker = _bare_worker()
    snapshot = {
        "status": "normal",
        "controller_mode": "normal",
        "reasons": [],
    }
    observed = []
    monkeypatch.setattr(
        Worker,
        "maintain_heartbeats",
        lambda _self, job: ("base-heartbeat", job),
    )
    monkeypatch.setattr(
        resource_aware_worker,
        "get_resource_pressure_snapshot_sync",
        lambda redis_client=None: snapshot,
    )
    worker._publish_pressure_state = lambda value: observed.append(value) or {
        "hard_gate_active": False,
        "soft_scale": 1.0,
        "cgroup_deltas": {},
    }
    job = object()

    result = worker.maintain_heartbeats(job)

    assert result == ("base-heartbeat", job)
    assert observed == [snapshot]
