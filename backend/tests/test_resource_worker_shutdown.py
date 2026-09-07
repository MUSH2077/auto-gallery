"""Real RQ signals and registration in fresh, owned Redis test namespaces."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import redis
from rq import Queue, get_current_job
from rq.registry import FailedJobRegistry


def mark_execution(key, release_key=None):
    connection = get_current_job().connection
    connection.incr(key)
    if release_key:
        deadline = time.monotonic() + 15
        while not connection.get(release_key):
            if time.monotonic() >= deadline:
                raise TimeoutError("Test did not release its owned workhorse")
            time.sleep(0.02)
    return "executed"


def run_worker(namespace, mode):
    """Only pressure input/domain projection are substituted; RQ is real."""
    from app.services import resource_aware_worker as module

    connection = redis.Redis.from_url(os.environ["REDIS_URL"])
    assert urlsplit(os.environ["REDIS_URL"]).path == "/15"
    assert namespace.startswith("shutdown-")
    module.get_resource_pressure_snapshot_sync = lambda **_: {
        "status":"paused" if connection.get(namespace + ":pressure") else "normal",
        "reasons":["test_pressure"] if connection.get(namespace + ":pressure") else [],
    }
    module._set_resource_state_sync = lambda *_, **__: None
    original_get = redis.client.PubSub.get_message
    if mode == "acquired":
        original_lock = module.local_lock_for_workload

        def lock_for(workload):
            lock = original_lock(workload)
            acquire = lock.try_acquire

            def acquire_and_signal():
                acquired = acquire()
                if acquired:
                    connection.set(namespace + ":acquired", "1")
                    os.kill(os.getpid(), signal.SIGTERM)
                return acquired

            lock.try_acquire = acquire_and_signal
            return lock

        module.local_lock_for_workload = lock_for

    def get_message(pubsub, *args, **kwargs):
        channels = {v.decode() if isinstance(v, bytes) else v for v in pubsub.channels}
        if module.RESOURCE_CONTROL_CHANNEL in channels:
            connection.incr(namespace + ":waiting")
        return original_get(pubsub, *args, **kwargs)

    redis.client.PubSub.get_message = get_message

    class TestWorker(module.ResourceAwareWorker):
        def _publish_pressure_state(self, snapshot):
            return {"hard_gate_active":False, "soft_scale":1.0, "cgroup_deltas":{}}

        def run_maintenance_tasks(self):
            # Do not scan or clean another test's queues/registrations.
            if mode == "aged" and getattr(self, "_resource_admission_active", False):
                queue.intermediate_queue.cleanup(self, queue)

        @property
        def should_run_maintenance_tasks(self):
            return True

        def _profile_admission(self, job, workload):
            if mode in {"accepted", "acquired", "aged"}:
                connection.set(namespace + ":pressure", "1")
                connection.set(namespace + ":accepted", job.id)
                if mode == "aged":
                    connection.set(queue.intermediate_queue.get_first_seen_key(job.id), time.time() - 120)
            return super()._profile_admission(job, workload)

    queue = Queue("downloads:" + namespace, connection=connection)
    worker = TestWorker([queue], connection=connection, name=namespace + "-worker")
    worker.work(with_scheduler=True)


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Owned worker did not reach the expected lifecycle boundary")


@pytest.fixture
def worker_process(tmp_path):
    assert urlsplit(os.environ["REDIS_URL"]).path == "/15"
    assert urlsplit(os.environ["DATABASE_URL"]).path.endswith("_test")
    namespace = "shutdown-" + uuid4().hex
    connection = redis.Redis.from_url(os.environ["REDIS_URL"])
    queue = Queue("downloads:" + namespace, connection=connection)
    processes = []

    def start(mode):
        connection.delete(namespace + ":waiting")
        if mode == "paused":
            connection.set(namespace + ":pressure", "1")
        log = (tmp_path / f"{mode}.log").open("w+")
        process = subprocess.Popen(
            [sys.executable, "-c", "from tests.test_resource_worker_shutdown import run_worker; "
             "import sys; run_worker(sys.argv[1],sys.argv[2])", namespace, mode],
            env={**os.environ, "HEAVY_IO_LOCK_PATH":str(tmp_path / "heavy-io.lock")},
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        processes.append((process, log))
        wait_for(lambda: connection.get(namespace + (
            ":executed" if mode == "running" else ":waiting"
        )))
        return process, log

    try:
        yield namespace, connection, queue, start
    finally:
        # Exact process groups created here only; never signal services or RQ
        # workers belonging to another namespace, including acceptance Redis10.
        for process, log in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            log.close()
        keys = list(connection.scan_iter(match="*" + namespace + "*"))
        if keys:
            connection.delete(*keys)
        connection.srem("rq:queues", queue.key)
        connection.srem("rq:workers", "rq:worker:" + namespace + "-worker")
        connection.close()


@pytest.mark.parametrize("mode", ["idle", "paused"])
def test_sigterm_exits_waiting_worker_and_unregisters_without_failed_jobs(worker_process, mode):
    namespace, connection, queue, start = worker_process
    if mode == "paused":
        queue.enqueue(mark_execution, namespace + ":executed", job_id=namespace + "-job")
    process, log = start(mode)
    process.send_signal(signal.SIGTERM)
    try:
        code = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.flush()
        pytest.fail("SIGTERM was swallowed; worker still alive:\n" + Path(log.name).read_text())
    assert code == 0
    assert not connection.sismember("rq:workers", "rq:worker:" + namespace + "-worker")
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0
    assert connection.get(namespace + ":executed") is None
    assert queue.count == (1 if mode == "paused" else 0)


def test_sigterm_during_admission_returns_unstarted_job_to_queue(worker_process):
    namespace, connection, queue, start = worker_process
    job = queue.enqueue(mark_execution, namespace + ":executed", job_id=namespace + "-job")
    process, _log = start("accepted")
    assert connection.get(namespace + ":accepted") == job.id.encode()
    assert queue.count == 0
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=5) == 0
    assert queue.get_job_ids() == [job.id]
    assert connection.lrange(queue.intermediate_queue.key, 0, -1) == []
    assert job.get_status(refresh=True).value == "queued"
    assert connection.get(namespace + ":executed") is None
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0

    assert not connection.sismember("rq:workers", "rq:worker:" + namespace + "-worker")
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    connection.delete(namespace + ":pressure")
    successor, _ = start("successor")
    wait_for(lambda: job.get_status(refresh=True).value == "finished")
    successor.send_signal(signal.SIGTERM)
    assert successor.wait(timeout=5) == 0
    assert connection.get(namespace + ":executed") == b"1"
    assert queue.count == 0
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0
    assert not connection.sismember("rq:workers", "rq:worker:" + namespace + "-worker")
    with pytest.raises(ProcessLookupError):
        os.killpg(successor.pid, 0)


def test_sigterm_finishes_running_job_before_normal_exit(worker_process):
    namespace, connection, queue, start = worker_process
    job = queue.enqueue(
        mark_execution, namespace + ":executed", namespace + ":release",
        job_id=namespace + "-job",
    )
    process, _ = start("running")
    process.send_signal(signal.SIGTERM)
    time.sleep(0.1)
    assert process.poll() is None
    connection.set(namespace + ":release", "1")
    assert process.wait(timeout=5) == 0
    assert job.get_status(refresh=True).value == "finished"
    assert connection.get(namespace + ":executed") == b"1"
    assert queue.count == 0
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0
    assert not connection.sismember("rq:workers", "rq:worker:" + namespace + "-worker")
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_stop_after_wait_and_lock_acquisition_finishes_owned_job(worker_process):
    namespace, connection, queue, start = worker_process
    job = queue.enqueue(mark_execution, namespace + ":executed", job_id=namespace + "-job")
    process, _ = start("acquired")
    connection.delete(namespace + ":pressure")
    assert process.wait(timeout=5) == 0
    assert connection.get(namespace + ":acquired") == b"1"
    assert job.get_status(refresh=True).value == "finished"
    assert connection.get(namespace + ":executed") == b"1"
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0
    assert queue.count == 0
    assert not connection.sismember("rq:workers", "rq:worker:" + namespace + "-worker")
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_admission_wait_defers_own_cleanup_and_clears_old_intermediate_age(worker_process):
    namespace, connection, queue, start = worker_process
    job = queue.enqueue(mark_execution, namespace + ":executed", job_id=namespace + "-job")
    process, _ = start("aged")
    assert job.get_status(refresh=True).value == "queued"
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=5) == 0
    assert queue.get_job_ids() == [job.id]
    assert connection.get(queue.intermediate_queue.get_first_seen_key(job.id)) is None
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0


def test_cancelled_admission_waiter_is_retired_without_resurrection(worker_process):
    namespace, connection, queue, start = worker_process
    job = queue.enqueue(mark_execution, namespace + ":executed", job_id=namespace + "-job")
    process, _ = start("aged")
    job.cancel()
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=5) == 0
    assert job.get_status(refresh=True).value == "canceled"
    assert queue.count == 0
    assert connection.lrange(queue.intermediate_queue.key, 0, -1) == []
    assert connection.get(queue.intermediate_queue.get_first_seen_key(job.id)) is None
    assert connection.get(namespace + ":executed") is None
    assert connection.zcard(FailedJobRegistry(queue=queue).key) == 0
