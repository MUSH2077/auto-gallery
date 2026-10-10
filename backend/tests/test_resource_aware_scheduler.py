import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import resource_aware_scheduler as scheduler_module
from app.services.resource_aware_scheduler import ResourceAwareScheduler


class FakeRegistry:
    def __init__(self, name, job_ids):
        self.name = name
        self.job_ids = job_ids
        self.timestamps = []

    def get_jobs_to_schedule(self, timestamp):
        self.timestamps.append(timestamp)
        return list(self.job_ids)


def _scheduled_noop():
    return None


def _scheduler(registries=(), *, acquired_locks=()):
    scheduler = object.__new__(ResourceAwareScheduler)
    scheduler._scheduled_job_registries = list(registries)
    scheduler._acquired_locks = set(acquired_locks)
    scheduler._connection = SimpleNamespace(name="redis")
    return scheduler


def test_due_queue_is_promoted_once_and_woken_once(monkeypatch):
    registry = FakeRegistry("imports", ["job-1"])
    scheduler = _scheduler([registry])
    promotions = []
    wakes = []

    def promote(_self):
        promotions.append(True)
        return "promoted"

    monkeypatch.setattr(scheduler_module.RQScheduler, "enqueue_scheduled_jobs", promote)
    monkeypatch.setattr(
        scheduler_module,
        "notify_queue_worker",
        lambda queue_name, connection: wakes.append((queue_name, connection)),
    )

    assert scheduler.enqueue_scheduled_jobs() == "promoted"
    assert promotions == [True]
    assert wakes == [("imports", scheduler.connection)]
    assert len(registry.timestamps) == 1
    assert isinstance(registry.timestamps[0], int)


def test_future_or_empty_queue_does_not_wake_worker(monkeypatch):
    registry = FakeRegistry("downloads", [])
    scheduler = _scheduler([registry])
    promotions = []
    wakes = []

    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: promotions.append(True),
    )
    monkeypatch.setattr(
        scheduler_module,
        "notify_queue_worker",
        lambda *args: wakes.append(args),
    )

    scheduler.enqueue_scheduled_jobs()

    assert promotions == [True]
    assert wakes == []


def test_duplicate_registry_entries_emit_one_queue_hint(monkeypatch):
    registries = [
        FakeRegistry("operations", ["job-1"]),
        FakeRegistry("operations", ["job-2"]),
    ]
    scheduler = _scheduler(registries)
    wakes = []

    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: None,
    )
    monkeypatch.setattr(
        scheduler_module,
        "notify_queue_worker",
        lambda queue_name, connection: wakes.append(queue_name),
    )

    scheduler.enqueue_scheduled_jobs()

    assert wakes == ["operations"]


def test_due_read_failure_does_not_block_original_promotion(monkeypatch):
    scheduler = _scheduler([SimpleNamespace(name="maintenance")])
    promoted = []

    def read_failure(_timestamp):
        raise RuntimeError("redis unavailable")

    scheduler._scheduled_job_registries[0].get_jobs_to_schedule = read_failure
    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: promoted.append(True) or "accepted",
    )

    assert scheduler.enqueue_scheduled_jobs() == "accepted"
    assert promoted == [True]


def test_hint_failure_leaves_promoted_result_intact(monkeypatch):
    import redis

    scheduler = _scheduler([FakeRegistry("maintenance", ["job-1"])])
    connection = redis.Redis.from_url(os.environ["REDIS_URL"])
    scheduler._connection = connection
    monkeypatch.setattr(scheduler_module.RQScheduler, "enqueue_scheduled_jobs", lambda _self: "accepted")
    monkeypatch.setattr(connection, "publish", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("subscriber unavailable")))

    assert scheduler.enqueue_scheduled_jobs() == "accepted"


@pytest.mark.integration
def test_real_redis_scheduled_promotion_preserves_future_and_wakes_once():
    import redis
    from rq import Queue
    from rq.job import Job
    from rq.registry import ScheduledJobRegistry

    connection = redis.Redis.from_url(os.environ["REDIS_URL"])
    queue_name = "operations"
    queue = Queue(queue_name, connection=connection)
    registry = ScheduledJobRegistry(queue_name, connection=connection)
    suffix = uuid4().hex
    due_ids = [f"scheduler-due-{suffix}-1", f"scheduler-due-{suffix}-2"]
    future_id = f"scheduler-future-{suffix}"
    jobs = []
    pubsub = connection.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("resource:work:light")
    scheduler = ResourceAwareScheduler([queue_name], connection=connection, interval=1)
    try:
        now = datetime.now(timezone.utc)
        for job_id in due_ids:
            jobs.append(queue.enqueue_at(now - timedelta(seconds=2), _scheduled_noop, job_id=job_id))
        future_job = queue.enqueue_at(now + timedelta(hours=1), _scheduled_noop, job_id=future_id)
        jobs.append(future_job)

        pubsub.get_message(timeout=1)
        scheduler.acquire_locks()
        scheduler.enqueue_scheduled_jobs()

        queued_ids = queue.get_job_ids()
        assert [queued_ids.count(job_id) for job_id in due_ids] == [1, 1]
        assert registry.get_job_ids(cleanup=False) == [future_id]
        message = pubsub.get_message(timeout=1)
        assert message is not None
        assert message["channel"] == b"resource:work:light"
        assert message["data"] == b"queued"
    finally:
        scheduler.release_locks()
        pubsub.close()
        for job in jobs:
            try:
                registry.remove(job.id)
            except Exception:
                pass
            try:
                Job.fetch(job.id, connection=connection).delete()
            except Exception:
                pass
        connection.close()


def test_initial_registry_prepare_failure_falls_back_to_rq(monkeypatch):
    scheduler = _scheduler(acquired_locks={"imports"})
    promoted = []

    def prepare_failure():
        raise RuntimeError("redis unavailable")

    scheduler.prepare_registries = prepare_failure
    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: promoted.append(True),
    )

    scheduler.enqueue_scheduled_jobs()

    assert promoted == [True]
