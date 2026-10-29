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


def test_download_registry_promotes_resource_waits_before_rq(monkeypatch):
    """The download-only wake path must still leave queue promotion to RQ."""

    registry = FakeRegistry("downloads:danbooru", [])
    scheduler = _scheduler([registry], acquired_locks={"downloads:danbooru"})
    calls = []

    from app.services import download_resource_wait
    monkeypatch.setattr(
        download_resource_wait,
        "promote_resource_waits",
        lambda connection, registries: calls.append((connection, registries)),
    )
    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: calls.append("rq") or "accepted",
    )
    monkeypatch.setattr(scheduler_module, "notify_queue_worker", lambda *args: None)

    assert scheduler.enqueue_scheduled_jobs() == "accepted"
    assert calls[0][1] == [registry]
    assert calls[-1] == "rq"


def test_non_download_registry_skips_resource_wait_promotion(monkeypatch):
    registry = FakeRegistry("imports", [])
    scheduler = _scheduler([registry], acquired_locks={"imports"})
    calls = []

    from app.services import download_resource_wait
    monkeypatch.setattr(
        download_resource_wait,
        "promote_resource_waits",
        lambda *args: calls.append("resource"),
    )
    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: calls.append("rq") or "accepted",
    )

    assert scheduler.enqueue_scheduled_jobs() == "accepted"
    assert calls == ["rq"]


def test_resource_wait_promotion_failure_does_not_change_non_download_contract(monkeypatch):
    """A broken optional wake check must not replace normal RQ promotion."""

    registry = FakeRegistry("imports", [])
    scheduler = _scheduler([registry], acquired_locks={"imports"})
    promoted = []
    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "enqueue_scheduled_jobs",
        lambda _self: promoted.append(True) or "accepted",
    )

    # The optional helper is not called for ordinary queues; RQ remains the
    # only promotion path even when its optional symbol is unavailable.
    from app.services import download_resource_wait
    monkeypatch.delattr(download_resource_wait, "promote_resource_waits", raising=False)
    assert scheduler.enqueue_scheduled_jobs() == "accepted"
    assert promoted == [True]


def _scheduled_counter(counter_key, fail_first=False):
    import redis
    connection=redis.Redis.from_url(os.environ['REDIS_URL'])
    try:
        attempt=connection.incr(counter_key)
        if fail_first and attempt==1:
            raise ValueError('retry once')
        return attempt
    finally:
        connection.close()


@pytest.mark.integration
def test_preloaded_worker_promotes_retries_and_recovers_scheduler(tmp_path):
    import re
    import signal
    import subprocess
    import sys
    import time
    import redis
    from rq import Queue, Retry
    from rq.job import JobStatus
    from rq.registry import ScheduledJobRegistry

    connection=redis.Redis.from_url(os.environ['REDIS_URL'])
    suffix=uuid4().hex
    queue_name='memory-scheduler-'+suffix
    queue=Queue(queue_name,connection=connection)
    counter='test:memory-scheduler:'+suffix
    retry_counter=counter+':retry'
    log_path=tmp_path/'worker.log'
    worker_source=(
        "import os,sys;from redis import Redis;"
        "from app.services.resource_aware_worker import ResourceAwareWorker;"
        "w=ResourceAwareWorker([sys.argv[1]],connection=Redis.from_url(os.environ['REDIS_URL']),"
        "maintenance_interval=1,worker_ttl=20);w.work(with_scheduler=True)"
    )
    log=log_path.open('w')
    process=subprocess.Popen([sys.executable,'-u','-c',worker_source,queue_name],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONPATH':os.path.dirname(__file__)+os.pathsep+os.environ.get('PYTHONPATH','')})
    jobs=[]
    def until(condition,seconds=20):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if condition():return
            assert process.poll() is None,log_path.read_text()[-2500:]
            time.sleep(.05)
        raise AssertionError(log_path.read_text()[-2500:])
    def schedulers():
        return [int(x)for x in re.findall(r'Scheduler for .* started with PID (\d+)',log_path.read_text())]
    try:
        until(lambda:bool(schedulers()))
        due=queue.enqueue_at(datetime.now(timezone.utc)+timedelta(seconds=1),_scheduled_counter,counter)
        jobs.append(due)
        retry=queue.enqueue_at(datetime.now(timezone.utc)+timedelta(seconds=1),_scheduled_counter,retry_counter,True,retry=Retry(max=1,interval=1))
        jobs.append(retry)
        until(lambda:due.get_status(refresh=True)==JobStatus.FINISHED and retry.get_status(refresh=True)==JobStatus.FINISHED)
        assert int(connection.get(counter))==1
        assert int(connection.get(retry_counter))==2
        old_pid=schedulers()[-1]
        os.kill(old_pid,signal.SIGKILL)
        recovered=queue.enqueue_at(datetime.now(timezone.utc)+timedelta(seconds=1),_scheduled_counter,counter)
        jobs.append(recovered)
        until(lambda:recovered.get_status(refresh=True)==JobStatus.FINISHED)
        assert schedulers()[-1]!=old_pid
        assert int(connection.get(counter))==2
        assert ScheduledJobRegistry(queue_name,connection=connection).count==0
    finally:
        process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)
        log.close()
        for job in jobs:
            job.delete()
        queue.delete(delete_jobs=True)
        connection.delete(counter,retry_counter)
        connection.close()
    assert process.returncode==0,log_path.read_text()[-2500:]



def test_non_download_scheduler_uses_original_rq_work(monkeypatch):
    scheduler = _scheduler(acquired_locks={"imports"})
    scheduler._queue_names = {"imports"}
    called = []
    monkeypatch.setattr(
        scheduler_module.RQScheduler,
        "work",
        lambda _self: called.append("rq"),
    )
    scheduler.work()
    assert called == ["rq"]


def test_download_scheduler_notification_disconnect_returns_to_interval(monkeypatch):
    from redis.exceptions import ConnectionError

    class PubSub:
        def __init__(self):
            self.closed = False

        def subscribe(self, channel):
            self.channel = channel

        def get_message(self, timeout):
            raise ConnectionError("subscriber disconnected")

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.pubsub_instance = PubSub()

        def pubsub(self, **kwargs):
            return self.pubsub_instance

    scheduler = _scheduler(acquired_locks={"downloads:danbooru"})
    scheduler._queue_names = {"downloads:danbooru"}
    scheduler.interval = 0.01
    scheduler._stop_requested = False
    connection = Connection()
    scheduler._connection = connection
    calls = []
    monkeypatch.setattr(scheduler, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(scheduler, "register_birth", lambda: None)
    monkeypatch.setattr(scheduler, "enqueue_scheduled_jobs", lambda: calls.append("enqueue"))
    monkeypatch.setattr(scheduler, "heartbeat", lambda: calls.append("heartbeat"))

    def stop():
        calls.append("stop")
        scheduler._stop_requested = True

    monkeypatch.setattr(scheduler, "stop", stop)
    sleeps = []
    def sleep(value):
        sleeps.append(value)
        scheduler._stop_requested = True
    monkeypatch.setattr(scheduler_module.time, "sleep", sleep)
    scheduler.work()

    assert calls == ["enqueue", "heartbeat", "stop"]
    assert sleeps == [scheduler.interval]
    assert connection.pubsub_instance.closed
