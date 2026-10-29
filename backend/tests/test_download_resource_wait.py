"""Redis/RQ correctness checks for resource-deferred download retries."""

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from time import time

import pytest

pytestmark = pytest.mark.integration


def _noop():
    return None


def _redis():
    import redis

    return redis.Redis.from_url(os.environ["REDIS_URL"])


def _future_job(connection, queue_name):
    from rq import Queue
    from rq.registry import ScheduledJobRegistry

    queue = Queue(queue_name, connection=connection)
    job_id = f"t10-resource-{uuid4().hex}"
    job = queue.enqueue_at(
        datetime.now(timezone.utc) + timedelta(hours=1),
        _noop,
        job_id=job_id,
    )
    # A scheduled job produced by RQ's retry path has a retry counter.
    # Set it explicitly so the CAS test models a completed retry, rather than
    # weakening the production guard for ordinary scheduled jobs.
    job.number_of_retries = 1
    job.save()
    return queue, ScheduledJobRegistry(queue_name, connection=connection), job


def _cleanup(connection, registry, job):
    try:
        registry.remove(job.id)
    except Exception:
        pass
    try:
        job.delete()
    except Exception:
        pass
    connection.delete(job.key)


def test_resource_retry_context_is_download_only():
    from rq import Retry
    from app.services.download_resource_wait import (
        RESOURCE_RETRY_CONTEXT,
        attach_resource_retry_context,
    )

    ordinary = Retry(max=3, interval=10)
    assert attach_resource_retry_context(ordinary, workload="import_db", reason="profile_busy") is ordinary
    assert not hasattr(ordinary, RESOURCE_RETRY_CONTEXT)

    platform = Retry(max=3, interval=10)
    assert attach_resource_retry_context(platform, workload="download_network", reason="http_rate_limit") is platform
    assert not hasattr(platform, RESOURCE_RETRY_CONTEXT)

    resource = Retry(max=3, interval=10)
    attach_resource_retry_context(
        resource,
        workload="download_network",
        reason="redis_lease_busy",
        source_identity="gallerydl-archive:test",
    )
    assert getattr(resource, RESOURCE_RETRY_CONTEXT) == {
        "workload": "download_network",
        "reason": "redis_lease_busy",
        "source_identity": "gallerydl-archive:test",
    }


def test_marker_is_registered_only_after_scheduled_score_exists():
    from rq import Retry
    from app.services.download_resource_wait import (
        RESOURCE_RETRY_CONTEXT,
        RESOURCE_WAIT_MARKER,
        attach_resource_retry_context,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="profile_busy",
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        raw = connection.hget(job.key, RESOURCE_WAIT_MARKER)
        marker = json.loads(raw)
        assert marker["job_id"] == job.id
        assert marker["retry_count"] == int(job.number_of_retries or 0)
        assert marker["scheduled_score"] == float(connection.zscore(registry.key, job.id))

        # A normal queue is never enrolled even if a caller supplies the marker.
        normal = f"imports:t10:{uuid4().hex}"
        assert not register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=normal
        )
    finally:
        _cleanup(connection, registry, job)
        connection.close()


def test_promotion_cas_clears_marker_and_leaves_rq_to_enqueue(monkeypatch):
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    from app.services.download_resource_wait import (
        RESOURCE_WAIT_MARKER,
        attach_resource_retry_context,
        promote_resource_waits,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="redis_lease_busy",
            source_identity=None,
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        before = float(connection.zscore(registry.key, job.id))
        monkeypatch.setattr(wait_module, "resource_wait_ready", lambda *_args: True)
        now = time() - 1
        assert promote_resource_waits(connection, [registry], now=now) == 1
        assert float(connection.zscore(registry.key, job.id)) == now
        assert connection.hget(job.key, RESOURCE_WAIT_MARKER) is None
        # No queue list entry is created by the wake helper; normal RQ promotion
        # remains the only owner that moves the scheduled job to ready.
        assert job.id not in queue.get_job_ids()
    finally:
        _cleanup(connection, registry, job)
        connection.close()


def test_busy_resource_does_not_advance_scheduled_job(monkeypatch):
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    from app.services.download_resource_wait import (
        attach_resource_retry_context,
        promote_resource_waits,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="resource_pressure",
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        before = float(connection.zscore(registry.key, job.id))
        monkeypatch.setattr(wait_module, "resource_wait_ready", lambda *_args: False)
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 0
        assert float(connection.zscore(registry.key, job.id)) == before
    finally:
        _cleanup(connection, registry, job)
        connection.close()


def test_stale_retry_marker_cannot_promote_new_retry(monkeypatch):
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    from app.services.download_resource_wait import (
        RESOURCE_WAIT_MARKER,
        attach_resource_retry_context,
        promote_resource_waits,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="profile_busy",
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        marker_raw = connection.hget(job.key, RESOURCE_WAIT_MARKER)
        marker = json.loads(marker_raw)
        # Simulate a newer retry/score replacing the old marker. CAS must leave it.
        marker["retry_count"] = marker["retry_count"] + 1
        connection.hset(job.key, RESOURCE_WAIT_MARKER, json.dumps(marker))
        monkeypatch.setattr(wait_module, "resource_wait_ready", lambda *_args: True)
        before = float(connection.zscore(registry.key, job.id))
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 0
        assert float(connection.zscore(registry.key, job.id)) == before
        assert json.loads(connection.hget(job.key, RESOURCE_WAIT_MARKER))["retry_count"] == marker["retry_count"]
    finally:
        _cleanup(connection, registry, job)
        connection.close()



def test_registration_requires_scheduled_status_and_registry_score():
    from rq import Retry
    from app.services.download_resource_wait import (
        RESOURCE_WAIT_MARKER,
        attach_resource_retry_context,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="profile_busy",
        )
        original_score = connection.zscore(registry.key, job.id)
        registry.remove(job.id)
        assert not register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        assert connection.hget(job.key, RESOURCE_WAIT_MARKER) is None

        connection.zadd(registry.key, {job.id: original_score})
        job.set_status("canceled")
        assert not register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        assert connection.hget(job.key, RESOURCE_WAIT_MARKER) is None
    finally:
        _cleanup(connection, registry, job)
        connection.close()


def test_cancelled_or_deleted_job_is_never_promoted(monkeypatch):
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    from app.services.download_resource_wait import (
        RESOURCE_WAIT_MARKER,
        attach_resource_retry_context,
        promote_resource_waits,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="profile_busy",
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        monkeypatch.setattr(wait_module, "resource_wait_ready", lambda *_args: True)
        connection.hset(job.key, "status", "canceled")
        before = float(connection.zscore(registry.key, job.id))
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 0
        assert float(connection.zscore(registry.key, job.id)) == before
        assert connection.hget(job.key, RESOURCE_WAIT_MARKER) is not None

        # A deleted hash cannot satisfy the marker/status CAS either.
        connection.delete(job.key)
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 0
        assert float(connection.zscore(registry.key, job.id)) == before
    finally:
        _cleanup(connection, registry, job)
        connection.close()


def test_repeated_wake_then_normal_rq_promotion_enqueues_once(monkeypatch):
    from rq import Queue
    from rq.scheduler import RQScheduler
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    from app.services.download_resource_wait import (
        attach_resource_retry_context,
        promote_resource_waits,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    scheduler = RQScheduler([queue.name], connection=connection, interval=1)
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="redis_lease_busy",
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        monkeypatch.setattr(wait_module, "resource_wait_ready", lambda *_args: True)
        before = float(connection.zscore(registry.key, job.id))
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 1
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 0
        scheduler.acquire_locks()
        scheduler.enqueue_scheduled_jobs()
        assert queue.get_job_ids().count(job.id) == 1
    finally:
        if scheduler.acquired_locks:
            scheduler.release_locks()
        _cleanup(connection, registry, job)
        connection.close()


def test_memory_pressure_keeps_resource_marker_scheduled(monkeypatch):
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    from app.services.download_resource_wait import (
        attach_resource_retry_context,
        promote_resource_waits,
        register_after_retry_for_registry,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="resource_pressure",
        )
        assert register_after_retry_for_registry(
            connection, job, retry, registry=registry, queue_name=queue.name
        )
        monkeypatch.setattr("app.services.resource_pressure.get_resource_pressure_snapshot_sync", lambda **_kwargs: {"memory": {"available_bytes": 0}})
        monkeypatch.setattr("app.services.resource_pressure.resource_profile_permit", lambda *_args: (False, {"reason": "resource_pressure"}))
        before = float(connection.zscore(registry.key, job.id))
        assert promote_resource_waits(connection, [registry], now=time() - 1) == 0
        assert float(connection.zscore(registry.key, job.id)) == before
    finally:
        _cleanup(connection, registry, job)
        connection.close()



def test_redis_source_key_loss_does_not_bypass_held_posix_lock(monkeypatch, tmp_path):
    from app.services import download_resource_wait as wait_module
    from app.services.heavy_io import local_source_lock
    from app.services.download_resource_wait import resource_wait_ready

    source = "gallerydl-archive:posix-held"
    monkeypatch.setattr("app.services.heavy_io._local_lock_path", lambda: tmp_path / "heavy-io.lock")
    monkeypatch.setattr("app.services.heavy_io.resource_reservation_details", lambda *_: (1, 8 * 1024**3))
    monkeypatch.setattr("app.services.resource_pressure.get_resource_pressure_snapshot_sync", lambda **_kwargs: {})
    monkeypatch.setattr("app.services.resource_pressure.resource_profile_permit", lambda *_args: (True, {}))
    connection = _redis()
    lock = local_source_lock(source)
    try:
        from app.services.heavy_io import source_lock_key
        connection.delete(source_lock_key(source))
        assert resource_wait_ready(connection, {"source_identity": source})
        assert lock.try_acquire()
        # Redis source key is deliberately absent; the authoritative local lock
        # must still keep the resource wait deferred.
        connection.delete(source_lock_key(source))
        assert not resource_wait_ready(connection, {"source_identity": source})
        lock.release()
        assert resource_wait_ready(connection, {"source_identity": source})
    finally:
        lock.release()
        connection.close()


def test_worker_registers_marker_after_rq_super_retry_commit(monkeypatch):
    from types import SimpleNamespace
    from rq import Retry
    from rq.worker import Worker
    from app.services import resource_aware_worker as worker_module
    from app.services.download_resource_wait import (
        RESOURCE_WAIT_MARKER,
        attach_resource_retry_context,
    )

    connection = _redis()
    queue, registry, job = _future_job(connection, f"downloads:t10:{uuid4().hex}")
    try:
        retry = attach_resource_retry_context(
            Retry(max=10, interval=10),
            workload="download_network",
            reason="profile_busy",
        )
        worker = object.__new__(worker_module.ResourceAwareWorker)
        worker.connection = connection
        seen = []

        def super_retry(_self, current_job, current_queue, *_args):
            # Model RQ's completed handle_job_retry: scheduled status and score
            # are visible before the subclass registers its advisory marker.
            current_job.set_status("scheduled")
            current_job.number_of_retries = 1
            current_job.save()
            seen.append(connection.zscore(registry.key, current_job.id))
            return "rq-result"

        monkeypatch.setattr(Worker, "handle_job_retry", super_retry)
        result = worker.handle_job_retry(
            job,
            queue,
            retry,
            SimpleNamespace(),
            SimpleNamespace(),
        )
        assert result == "rq-result"
        assert seen and seen[0] is not None
        assert connection.hget(job.key, RESOURCE_WAIT_MARKER) is not None
    finally:
        _cleanup(connection, registry, job)
        connection.close()


def test_native_download_alias_gets_resource_context(monkeypatch):
    from types import SimpleNamespace
    from rq import Retry
    from app.services import heavy_io
    from app.services.download_resource_wait import RESOURCE_RETRY_CONTEXT
    monkeypatch.setattr("rq.get_current_job", lambda: SimpleNamespace(id="download-alias"))
    monkeypatch.setattr(heavy_io, "_update_current_job_resource_meta", lambda *_: None)
    retry = heavy_io._defer_current_rq_job(
        heavy_io.HeavyIOUnavailable("redis_lease_busy"),
        workload="download", source_identity="gallerydl-archive:danbooru",
    )
    assert isinstance(retry, Retry)
    assert retry.intervals == [10]
    assert getattr(retry, RESOURCE_RETRY_CONTEXT)["source_identity"] == "gallerydl-archive:danbooru"
    ordinary = heavy_io._defer_current_rq_job(
        heavy_io.HeavyIOUnavailable("download_archive_identity_unavailable"), workload="download"
    )
    assert not hasattr(ordinary, RESOURCE_RETRY_CONTEXT)


@pytest.mark.parametrize("guard", ["legacy-network", "admission", "redis-slots"])
def test_existing_download_admission_guards_do_not_trigger_repeat_attempts(monkeypatch, tmp_path, guard):
    from app.config import settings
    from app.services import heavy_io, resource_pressure
    from app.services.download_resource_wait import resource_wait_ready
    monkeypatch.setattr(heavy_io, "_local_lock_path", lambda: tmp_path / "heavy-io.lock")
    monkeypatch.setattr(heavy_io, "resource_reservation_details", lambda *_: (1, 8 * 1024**3))
    monkeypatch.setattr(resource_pressure, "get_resource_pressure_snapshot_sync", lambda **_: {})
    monkeypatch.setattr(resource_pressure, "resource_profile_permit", lambda *_: (True, {}))
    monkeypatch.setattr(settings, "download_execution_concurrency", 2)
    monkeypatch.setattr(settings, "download_device_concurrency_limit", 5)
    monkeypatch.setattr(settings, "download_concurrency_cap", 5)
    connection = _redis()
    lock = heavy_io.LocalHeavyIOLock((tmp_path / "heavy-io.lock").with_name("network-admission.lock"))
    keys = []
    try:
        assert resource_wait_ready(connection, {})
        if guard == "legacy-network":
            keys = [heavy_io.RESOURCE_NETWORK_TOKEN_KEY]
            connection.set(keys[0], json.dumps({"reserved_bytes": 1}))
        elif guard == "admission":
            assert lock.try_acquire()
        else:
            keys = list(heavy_io.DOWNLOAD_RESERVATION_KEYS[:2])
            for key in keys:
                connection.set(key, json.dumps({"reserved_bytes": 1}))
        assert not resource_wait_ready(connection, {})
    finally:
        if keys: connection.delete(*keys)
        lock.release()
        assert resource_wait_ready(connection, {})
        connection.close()


def test_one_wake_per_archive_keeps_other_archives_parallel(monkeypatch):
    from rq import Retry
    from app.services import download_resource_wait as wait_module
    connection = _redis()
    entries = []
    try:
        for index, source in enumerate(("gallerydl-archive:a", "gallerydl-archive:a", "gallerydl-archive:b")):
            queue, registry, job = _future_job(connection, "downloads:t10-cohort")
            entries.append((registry, job))
            retry = wait_module.attach_resource_retry_context(Retry(max=3, interval=10), workload="download", reason="profile_busy", source_identity=source)
            assert wait_module.register_after_retry_for_registry(connection, job, retry, registry=registry, queue_name=queue.name)
        monkeypatch.setattr(wait_module, "resource_wait_ready", lambda *_: True)
        assert wait_module.promote_resource_waits(connection, [entries[0][0]], now=time() - 1) == 2
        assert sum(connection.hget(job.key, wait_module.RESOURCE_WAIT_MARKER) is not None for _, job in entries) == 1
    finally:
        for registry, job in entries:
            _cleanup(connection, registry, job)
        connection.close()
