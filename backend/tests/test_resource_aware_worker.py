from datetime import datetime, timezone

import redis
import pytest
from rq import Worker

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
