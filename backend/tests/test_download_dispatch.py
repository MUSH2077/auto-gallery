import asyncio
import inspect
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _RedisLock:
    def __init__(self, mutex):
        self.mutex = mutex

    def acquire(self, blocking=True):
        assert blocking is True
        return self.mutex.acquire(timeout=5)

    def release(self):
        self.mutex.release()


class _AdmissionRedis:
    def __init__(self):
        self.mutex = threading.Lock()
        self.jobs = {}
        self.waiting = 0

    def lock(self, _name, *, timeout, blocking_timeout):
        assert timeout >= 60
        assert blocking_timeout > 0
        return _RedisLock(self.mutex)

    def info(self, section):
        assert section == "memory"
        return {"used_memory": 1, "maxmemory": 1_000_000}

    def set(self, *_args, **_kwargs):
        return True

    def delete(self, *_args):
        return 1


def test_concurrent_download_admission_never_exceeds_hard_cap(monkeypatch):
    """The lock must serialize count+enqueue across immediate/delayed producers."""

    from app.services import backpressure

    redis = _AdmissionRedis()

    class Queue:
        def __init__(self, name, connection):
            self.name = name
            self.connection = connection

        def _accept(self, kwargs):
            job = SimpleNamespace(id=kwargs["job_id"], queue_name=self.name)
            self.connection.jobs[job.id] = job
            self.connection.waiting += 1
            return job

        def enqueue(self, _func, *_args, **kwargs):
            return self._accept(kwargs)

        def enqueue_in(self, _delay, _func, *_args, **kwargs):
            return self._accept(kwargs)

    monkeypatch.setattr("rq.Queue", Queue)
    monkeypatch.setattr(backpressure, "_download_queue_limit", lambda: 100)
    monkeypatch.setattr(
        backpressure,
        "_download_waiting_count",
        lambda client, **_kwargs: client.waiting,
    )
    monkeypatch.setattr(
        backpressure,
        "_existing_rq_job",
        lambda client, rq_job_id: client.jobs.get(rq_job_id),
    )

    def produce(index):
        try:
            job = backpressure.enqueue_download_rq(
                "downloads" if index % 2 == 0 else "downloads:pixiv",
                "app.jobs.download.run_download_job",
                str(index),
                rq_job_id=f"download-{index}-attempt-1",
                job_timeout=7200,
                delay_seconds=30 if index % 2 else None,
                redis_client=redis,
            )
            return job.id
        except backpressure.DownloadAdmissionError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=24) as pool:
        outcomes = list(pool.map(produce, range(140)))

    assert redis.waiting == 100
    assert len(redis.jobs) == 100
    assert outcomes.count("queue_saturated") == 40


def test_ambiguous_enqueue_response_is_resolved_by_deterministic_id(monkeypatch):
    from app.services import backpressure

    redis = _AdmissionRedis()

    class Queue:
        def __init__(self, name, connection):
            self.connection = connection

        def enqueue(self, _func, *_args, **kwargs):
            job = SimpleNamespace(id=kwargs["job_id"])
            self.connection.jobs[job.id] = job
            self.connection.waiting += 1
            raise TimeoutError("response lost after Redis EXEC")

    monkeypatch.setattr("rq.Queue", Queue)
    monkeypatch.setattr(
        backpressure,
        "_download_waiting_count",
        lambda client, **_kwargs: client.waiting,
    )
    monkeypatch.setattr(
        backpressure,
        "_existing_rq_job",
        lambda client, rq_job_id: client.jobs.get(rq_job_id),
    )

    rq_job = backpressure.enqueue_download_rq(
        "downloads",
        "app.jobs.download.run_download_job",
        "domain-job",
        rq_job_id="download-domain-job-attempt-1",
        job_timeout=7200,
        redis_client=redis,
    )
    assert rq_job.id == "download-domain-job-attempt-1"
    assert redis.waiting == 1

    # Retrying the same publication intent must return the existing job and
    # must not consume a second queue slot.
    same_job = backpressure.enqueue_download_rq(
        "downloads",
        "app.jobs.download.run_download_job",
        "domain-job",
        rq_job_id="download-domain-job-attempt-1",
        job_timeout=7200,
        redis_client=redis,
    )
    assert same_job is rq_job
    assert redis.waiting == 1


def test_waiting_count_includes_queued_intermediate_scheduled_and_deferred(monkeypatch):
    from app.services import backpressure

    intermediate = {"downloads": 2, "downloads:pixiv": 3}

    class Redis:
        def llen(self, key):
            queue_name = key.removeprefix("rq:queue:").removesuffix(":intermediate")
            return intermediate.get(queue_name, 0)

    redis = Redis()
    counts = {"downloads": 2, "downloads:pixiv": 1}
    scheduled = {"downloads": 3, "downloads:pixiv": 2}
    deferred = {"downloads": 4, "downloads:pixiv": 1}

    class Queue:
        def __init__(self, name, connection):
            assert connection is redis
            self.name = name

        @classmethod
        def all(cls, connection):
            assert connection is redis
            return [SimpleNamespace(name="downloads:pixiv")]

        @property
        def count(self):
            return counts.get(self.name, 0)

        @property
        def intermediate_queue_key(self):
            return f"rq:queue:{self.name}:intermediate"

    class ScheduledRegistry:
        def __init__(self, name, connection):
            assert connection is redis
            self.name = name

        @property
        def count(self):
            return scheduled.get(self.name, 0)

    class DeferredRegistry:
        def __init__(self, name, connection):
            assert connection is redis
            self.name = name

        @property
        def count(self):
            return deferred.get(self.name, 0)

    monkeypatch.setattr("rq.Queue", Queue)
    monkeypatch.setattr("rq.registry.ScheduledJobRegistry", ScheduledRegistry)
    monkeypatch.setattr("rq.registry.DeferredJobRegistry", DeferredRegistry)
    assert backpressure._download_waiting_count(redis) == 18


class _DispatchDB:
    def __init__(self):
        self.commit_count = 0
        self.flush_count = 0
        self.rollback_count = 0

    async def commit(self):
        self.commit_count += 1

    async def flush(self):
        self.flush_count += 1

    async def rollback(self):
        self.rollback_count += 1

    async def refresh(self, _obj):
        return None


def test_prepare_reuses_existing_task_run_and_uses_rq_safe_id(monkeypatch):
    from app.services import download_dispatch

    domain_id = uuid4()
    task = SimpleNamespace(
        id=uuid4(),
        attempts=3,
        queue_name="downloads",
        rq_job_id="old-id",
        status="failed",
        meta={"subscription_id": "preserved"},
    )
    job = SimpleNamespace(
        id=domain_id,
        source="pixiv",
        source_url="https://example.test/creator",
        progress_data={"stage": "enqueued"},
    )

    class TaskService:
        def __init__(self, _db):
            pass

        async def get_by_subject(self, subject_type, subject_id):
            assert (subject_type, subject_id) == ("download_job", domain_id)
            return task

        async def ensure_download_task(self, *_args, **_kwargs):
            raise AssertionError("existing TaskRun must not be inserted again")

        async def update_task(self, existing, **values):
            existing.status = values["status"]
            existing.rq_job_id = values["rq_job_id"]
            existing.meta = values["meta"]
            return existing

    monkeypatch.setattr(download_dispatch, "TaskService", TaskService)
    monkeypatch.setattr(download_dispatch, "append_manifest_event", lambda *_a, **_k: None)

    prepared = asyncio.run(
        download_dispatch.prepare_download_dispatch(
            _DispatchDB(),
            job,
            queue_name="downloads:pixiv",
            job_timeout=7200,
            action="retry",
        )
    )
    assert prepared.task is task
    assert prepared.attempt == 4
    assert prepared.rq_job_id == f"download-{domain_id}-attempt-4"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", prepared.rq_job_id)
    assert task.queue_name == "downloads:pixiv"
    assert task.meta["subscription_id"] == "preserved"
    assert task.meta[download_dispatch.DISPATCH_META_KEY] == {
        "version": 1,
        "state": download_dispatch.DISPATCH_PENDING,
        "queue_name": "downloads:pixiv",
        "rq_job_id": prepared.rq_job_id,
        "job_timeout": 7200,
        "delay_seconds": None,
        "action": "retry",
        "attempt": 4,
        "prepared_at": task.meta[download_dispatch.DISPATCH_META_KEY]["prepared_at"],
    }


def test_publish_commits_before_rq_and_compensates_both_models(monkeypatch):
    from app.services import download_dispatch
    from app.services.backpressure import DownloadAdmissionError

    db = _DispatchDB()
    job = SimpleNamespace(
        id=uuid4(),
        status="enqueued",
        error_log=None,
        pipeline_stage="enqueued",
        progress_data={"stage": "enqueued"},
    )
    rq_job_id = f"download-{job.id}-attempt-1"
    task = SimpleNamespace(
        status="enqueued",
        error_log=None,
        rq_job_id=rq_job_id,
        meta={
            "download_dispatch": {
                "state": "pending",
                "rq_job_id": rq_job_id,
                "attempt": 1,
            },
        },
    )
    prepared = download_dispatch.PreparedDownloadDispatch(
        task=task,
        queue_name="downloads",
        rq_job_id=rq_job_id,
        attempt=1,
    )

    def reject_after_check(*_args, **_kwargs):
        assert db.commit_count == 1, "Redis publication happened before the DB commit"
        raise DownloadAdmissionError(
            "queue_saturated",
            "Download queue is full",
            status_code=429,
        )

    class TaskService:
        def __init__(self, _db):
            pass

        async def update_task(self, existing, **values):
            existing.status = values["status"]
            existing.error_log = values["error"]
            existing.rq_job_id = values["rq_job_id"]
            existing.meta = values["meta"]
            return existing

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", reject_after_check)
    monkeypatch.setattr(download_dispatch, "TaskService", TaskService)
    monkeypatch.setattr(download_dispatch, "append_manifest_event", lambda *_a, **_k: None)

    with pytest.raises(DownloadAdmissionError) as caught:
        asyncio.run(
            download_dispatch.publish_prepared_download(
                db,
                job,
                prepared,
                job_timeout=7200,
            )
        )

    assert caught.value.code == "queue_saturated"
    assert db.commit_count == 2
    assert job.status == "failed"
    assert task.status == "failed"
    assert job.progress_data["stage"] == "failed"
    assert "queue is full" in task.error_log.lower()
    assert task.meta["download_dispatch"]["state"] == "failed"


def _pending_outbox_pair(download_dispatch):
    job_id = uuid4()
    rq_job_id = download_dispatch.deterministic_download_rq_job_id(job_id, 2)
    task = SimpleNamespace(
        id=uuid4(),
        status="enqueued",
        queue_name="downloads",
        rq_job_id=rq_job_id,
        error_log=None,
        result_data=None,
        meta={
            download_dispatch.DISPATCH_META_KEY: {
                "version": 1,
                "state": download_dispatch.DISPATCH_PENDING,
                "queue_name": "downloads",
                "rq_job_id": rq_job_id,
                "job_timeout": 7200,
                "delay_seconds": 30,
                "action": "auto_retry",
                "attempt": 2,
                "prepared_at": "2026-08-09T00:00:00+00:00",
            },
        },
    )
    job = SimpleNamespace(
        id=job_id,
        status="enqueued",
        error_log=None,
        pipeline_stage="enqueued",
        progress_data={"stage": "enqueued"},
    )
    return task, job


def test_outbox_recovery_replays_missing_rq_job_and_marks_published(monkeypatch):
    from app.services import download_dispatch

    db = _DispatchDB()
    task, job = _pending_outbox_pair(download_dispatch)
    calls = []

    monkeypatch.setattr(download_dispatch, "_fetch_download_rq_job", lambda *_a, **_k: None)

    def enqueue(queue_name, func, job_id, **kwargs):
        calls.append((queue_name, func, job_id, kwargs))
        return SimpleNamespace(id=kwargs["rq_job_id"])

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", enqueue)
    outcome = asyncio.run(
        download_dispatch.recover_download_dispatch_candidate(db, task, job)
    )
    assert outcome == "replayed"
    assert calls == [(
        "downloads",
        "app.jobs.download.run_download_job",
        str(job.id),
        {
            "rq_job_id": task.rq_job_id,
            "job_timeout": 7200,
            "delay_seconds": 30.0,
            "redis_client": None,
        },
    )]
    assert db.commit_count == 1
    assert task.status == "enqueued"
    assert job.status == "enqueued"
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["state"] == "published"


def test_outbox_recovery_transient_rejection_stays_pending(monkeypatch):
    from app.services import download_dispatch
    from app.services.backpressure import DownloadAdmissionError

    db = _DispatchDB()
    task, job = _pending_outbox_pair(download_dispatch)
    monkeypatch.setattr(download_dispatch, "_fetch_download_rq_job", lambda *_a, **_k: None)

    def reject(*_args, **_kwargs):
        raise DownloadAdmissionError(
            "queue_saturated",
            "Download queue is full",
            status_code=429,
        )

    monkeypatch.setattr(download_dispatch, "enqueue_download_rq", reject)
    outcome = asyncio.run(
        download_dispatch.recover_download_dispatch_candidate(db, task, job)
    )
    assert outcome == "deferred"
    assert db.commit_count == 0
    assert db.rollback_count == 1
    assert task.status == "enqueued"
    assert job.status == "enqueued"
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["state"] == "pending"


def test_outbox_recovery_existing_fixed_id_does_not_reenqueue(monkeypatch):
    from app.services import download_dispatch

    db = _DispatchDB()
    task, job = _pending_outbox_pair(download_dispatch)
    existing = SimpleNamespace(id=task.rq_job_id)
    monkeypatch.setattr(
        download_dispatch,
        "_fetch_download_rq_job",
        lambda *_a, **_k: existing,
    )
    monkeypatch.setattr(
        download_dispatch,
        "enqueue_download_rq",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not enqueue")),
    )
    outcome = asyncio.run(
        download_dispatch.recover_download_dispatch_candidate(db, task, job)
    )
    assert outcome == "existing"
    assert db.commit_count == 1
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["state"] == "published"


@pytest.mark.parametrize("rq_status", ["finished", "failed", "canceled"])
def test_outbox_recovery_terminal_rq_record_fails_both_models(monkeypatch, rq_status):
    from app.services import download_dispatch

    db = _DispatchDB()
    task, job = _pending_outbox_pair(download_dispatch)
    rq_record = SimpleNamespace(
        id=task.rq_job_id,
        description="app.jobs.download.run_download_job('domain-job')",
        exc_info="worker exited before durable download status update",
        ended_at="2026-08-09T01:02:03+00:00",
        get_status=lambda refresh=True: SimpleNamespace(value=rq_status),
    )
    events = []

    class TaskService:
        def __init__(self, _db):
            pass

        async def update_task(self, existing, **values):
            existing.status = values["status"]
            existing.error_log = values["error"]
            existing.result_data = values["result"]
            existing.meta = values["meta"]
            existing.rq_job_id = values["rq_job_id"]
            return existing

    async def lock_current_rows(_db, *, job_id, task_id):
        assert job_id == job.id
        assert task_id == task.id
        return job, task

    monkeypatch.setattr(
        download_dispatch,
        "_fetch_download_rq_job",
        lambda *_a, **_k: rq_record,
    )
    monkeypatch.setattr(
        download_dispatch,
        "enqueue_download_rq",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not replay")),
    )
    monkeypatch.setattr(download_dispatch, "TaskService", TaskService)
    monkeypatch.setattr(
        download_dispatch,
        "_locked_download_dispatch_rows",
        lock_current_rows,
    )
    monkeypatch.setattr(
        download_dispatch,
        "append_manifest_event",
        lambda *_args, **kwargs: events.append(kwargs),
    )

    outcome = asyncio.run(
        download_dispatch.recover_download_dispatch_candidate(db, task, job)
    )

    assert outcome == "terminal"
    assert db.commit_count == 1
    assert db.rollback_count == 1
    assert job.status == "failed"
    assert task.status == "failed"
    assert job.progress_data["stage"] == "failed"
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["state"] == "failed"
    assert task.meta[download_dispatch.DISPATCH_META_KEY]["terminal_rq_status"] == rq_status
    assert task.result_data["download_dispatch_failure"] == {
        "code": "rq_terminal_record",
        "rq_status": rq_status,
        "rq_description": "app.jobs.download.run_download_job('domain-job')",
        "rq_exc_info": "worker exited before durable download status update",
        "rq_ended_at": "2026-08-09T01:02:03+00:00",
        "rq_job_id": task.rq_job_id,
    }
    assert rq_status in task.error_log
    assert events == [{"rq_job_id": task.rq_job_id, "rq_status": rq_status}]


def test_terminal_rq_recovery_does_not_clobber_newer_dispatch_attempt(monkeypatch):
    from app.services import download_dispatch

    db = _DispatchDB()
    task, job = _pending_outbox_pair(download_dispatch)
    old_rq_job_id = task.rq_job_id
    terminal_record = SimpleNamespace(
        get_status=lambda refresh=True: SimpleNamespace(value="failed"),
    )

    async def lock_superseded_rows(_db, *, job_id, task_id):
        assert job_id == job.id
        assert task_id == task.id
        task.rq_job_id = download_dispatch.deterministic_download_rq_job_id(job.id, 3)
        task.meta[download_dispatch.DISPATCH_META_KEY]["rq_job_id"] = task.rq_job_id
        task.meta[download_dispatch.DISPATCH_META_KEY]["attempt"] = 3
        return job, task

    monkeypatch.setattr(
        download_dispatch,
        "_fetch_download_rq_job",
        lambda *_a, **_k: terminal_record,
    )
    monkeypatch.setattr(
        download_dispatch,
        "_locked_download_dispatch_rows",
        lock_superseded_rows,
    )
    monkeypatch.setattr(
        download_dispatch,
        "TaskService",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not persist")),
    )

    outcome = asyncio.run(
        download_dispatch.recover_download_dispatch_candidate(db, task, job)
    )

    assert outcome == "skipped"
    assert task.rq_job_id != old_rq_job_id
    assert task.status == "enqueued"
    assert job.status == "enqueued"
    assert db.commit_count == 0
    assert db.rollback_count == 2


def test_repository_sync_maps_saturation_to_http_429(monkeypatch):
    from app.api import repositories

    async def context(_db, _source_id):
        return object(), object(), object()

    async def saturated(*_args, **_kwargs):
        return {
            "status": "error",
            "skip_reason": "queue_saturated",
            "reason": {
                "code": "queue_saturated",
                "message": "Download queue is full",
                "details": {"queued": 100, "maximum_queued": 100},
            },
        }

    monkeypatch.setattr(repositories, "_get_source_context", context)
    monkeypatch.setattr(repositories, "enqueue_subscription_source_sync", saturated)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(repositories.sync_repository(uuid4(), db=object()))
    assert caught.value.status_code == 429
    assert caught.value.detail == {
        "code": "queue_saturated",
        "message": "Download queue is full",
        "queued": 100,
        "maximum_queued": 100,
    }


def test_subscription_conflict_uses_savepoint_not_outer_rollback():
    from app.services.subscription_enqueue import enqueue_subscription_source_sync

    source = inspect.getsource(enqueue_subscription_source_sync)
    assert "db.begin_nested()" in source
    assert "await db.rollback()" not in source
