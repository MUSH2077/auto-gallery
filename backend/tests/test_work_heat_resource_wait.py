"""Integration checks for work_heat resource deferral without Retry consumption."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from time import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _noop(source):
    return source


def _redis():
    import redis

    return redis.Redis.from_url(os.environ["REDIS_URL"])


def _claimed_job(connection):
    from rq import Queue
    from rq.registry import ScheduledJobRegistry

    queue = Queue("maintenance", connection=connection)
    queue.enqueue(
        "app.jobs.work_heat.run_work_heat_recompute",
        "pixiv",
        job_id=f"t-work-heat-resource-{uuid4().hex}",
    )
    claimed = Queue.dequeue_any([queue], timeout=1, connection=connection)
    assert claimed is not None
    job, claimed_queue = claimed
    first_seen = claimed_queue.intermediate_queue.get_first_seen_key(job.id)
    claimed_queue.intermediate_queue.set_first_seen(job.id)
    return queue, ScheduledJobRegistry(queue=queue), job, first_seen


def _cleanup(connection, registry, job, first_seen):
    try:
        registry.remove(job.id)
    except Exception:
        pass
    connection.delete(job.key, first_seen)
    from rq import Queue
    queue = Queue("maintenance", connection=connection)
    queue.intermediate_queue.remove(job.id)
    connection.lrem(queue.key, 0, job.id)
    connection.close()


def test_work_heat_resource_deferral_preserves_job_and_retry_metadata():
    from app.services.work_heat_queue import (
        WORK_HEAT_RESOURCE_WAIT_MARKER,
        defer_work_heat_for_resources,
    )

    connection = _redis()
    queue, registry, job, first_seen = _claimed_job(connection)
    try:
        before_retries = int(job.number_of_retries or 0)
        assert defer_work_heat_for_resources(
            job, queue, reason="profile_busy", delay_seconds=10
        )
        job.refresh()
        assert job.id not in queue.get_job_ids()
        assert connection.zscore(registry.key, job.id) is not None
        assert job.get_status(refresh=True).value == "scheduled"
        assert int(job.number_of_retries or 0) == before_retries
        marker = json.loads(connection.hget(job.key, WORK_HEAT_RESOURCE_WAIT_MARKER))
        assert marker["job_id"] == job.id
        assert marker["reason"] == "profile_busy"
    finally:
        _cleanup(connection, registry, job, first_seen)


def test_work_heat_resource_promotion_uses_marker_cas(monkeypatch):
    from app.services import work_heat_queue as module
    from app.services.work_heat_queue import (
        WORK_HEAT_RESOURCE_WAIT_MARKER,
        defer_work_heat_for_resources,
        promote_work_heat_resource_waits,
    )

    connection = _redis()
    queue, registry, job, first_seen = _claimed_job(connection)
    try:
        # Resource release can happen before the scheduled retry time.
        assert defer_work_heat_for_resources(job, queue, reason="resource_pressure", delay_seconds=60)
        monkeypatch.setattr(module, "_work_heat_resource_wait_ready", lambda _connection: True)
        assert promote_work_heat_resource_waits(connection, [registry], now=time()) == 1
        assert connection.hget(job.key, WORK_HEAT_RESOURCE_WAIT_MARKER) is None
        assert float(connection.zscore(registry.key, job.id)) <= time() + 1
        # A second scan cannot promote the same generation again.
        assert promote_work_heat_resource_waits(connection, [registry], now=time() + 2) == 0
    finally:
        _cleanup(connection, registry, job, first_seen)



def test_work_heat_resource_deferral_preserves_dependency_key():
    from app.services.work_heat_queue import defer_work_heat_for_resources

    connection = _redis()
    queue, registry, job, first_seen = _claimed_job(connection)
    try:
        connection.sadd(job.dependencies_key, "work-heat-parent")
        assert defer_work_heat_for_resources(job, queue, reason="remote_search_in_flight")
        assert connection.smembers(job.dependencies_key) == {b"work-heat-parent"}
    finally:
        _cleanup(connection, registry, job, first_seen)

def test_same_id_new_generation_rejects_old_job_park_and_clear(monkeypatch):
    from app.services import work_heat_queue as module
    from app.services.work_heat_queue import (
        WORK_HEAT_RESOURCE_WAIT_MARKER,
        clear_work_heat_resource_wait,
        defer_work_heat_for_resources,
    )
    from rq.registry import ScheduledJobRegistry

    connection = _redis()
    queue, registry, old_job, first_seen = _claimed_job(connection)
    job_id = old_job.id
    try:
        assert defer_work_heat_for_resources(old_job, queue, reason="profile_busy")
        old_marker = connection.hget(old_job.key, WORK_HEAT_RESOURCE_WAIT_MARKER)
        registry.remove(job_id)
        connection.delete(old_job.key, first_seen)
        new_job = queue.enqueue_at(
            datetime.now(timezone.utc) + timedelta(minutes=5),
            "app.jobs.work_heat.run_work_heat_recompute",
            "pixiv", job_id=job_id,
        )
        new_registry = ScheduledJobRegistry(queue=queue)
        new_marker = json.dumps({
            "job_id": job_id,
            "created_at": (connection.hget(new_job.key, "created_at") or b"").decode(),
            "scheduled_score": float(connection.zscore(new_registry.key, job_id)),
            "reason": "new-generation",
        }, separators=(",", ":"))
        connection.hset(new_job.key, WORK_HEAT_RESOURCE_WAIT_MARKER, new_marker)
        before_score = float(connection.zscore(new_registry.key, job_id))
        clear_work_heat_resource_wait(connection, old_job)
        assert connection.hget(new_job.key, WORK_HEAT_RESOURCE_WAIT_MARKER) == new_marker.encode()
        assert float(connection.zscore(new_registry.key, job_id)) == before_score
        assert old_marker != new_marker.encode()
    finally:
        _cleanup(connection, new_registry if "new_registry" in locals() else registry, new_job if "new_job" in locals() else old_job, first_seen)

def test_work_heat_wait_respects_pending_import_queue():
    from app.services import work_heat_queue as module

    connection = _redis()
    marker = f"rq:job:import-wait-{uuid4().hex}"
    try:
        connection.lpush("rq:queue:imports", marker)
        assert module._work_heat_resource_wait_ready(connection) is False
    finally:
        connection.lrem("rq:queue:imports", 0, marker)
        connection.close()



def test_running_primary_followup_stays_deferred_when_primary_is_parked(monkeypatch):
    from app.services import work_heat_queue as module
    from app.services.work_heat_queue import (
        WORK_HEAT_RESOURCE_WAIT_MARKER,
        defer_work_heat_for_resources,
        promote_work_heat_resource_waits,
        request_work_heat_recompute,
    )
    from rq.registry import DeferredJobRegistry, ScheduledJobRegistry

    connection = _redis()
    source = f"heat{uuid4().hex[:10]}"
    queue = __import__("rq").Queue("maintenance", connection=connection)
    primary_id = f"work-heat-{source}"
    followup_id = f"{primary_id}-followup"
    queue.enqueue("app.jobs.work_heat.run_work_heat_recompute", source, job_id=primary_id)
    claimed = queue.dequeue_any([queue], timeout=1, connection=connection)
    assert claimed is not None
    primary, _ = claimed
    primary.set_status("started")
    try:
        first = request_work_heat_recompute({source}, redis_client=connection)
        assert first["created"] == 1
        followup = queue.fetch_job(followup_id)
        assert followup is not None
        assert followup.get_status(refresh=True).value == "deferred"
        primary.set_status("queued")
        assert defer_work_heat_for_resources(primary, queue, reason="profile_busy", delay_seconds=60)
        second = request_work_heat_recompute({source}, redis_client=connection)
        assert second["coalesced"] == 1
        monkeypatch.setattr(module, "_work_heat_resource_wait_ready", lambda _connection: True)
        registry = ScheduledJobRegistry(queue=queue)
        assert promote_work_heat_resource_waits(connection, [registry], now=time()) == 1
        assert queue.fetch_job(followup_id).get_status(refresh=True).value == "deferred"
    finally:
        for job_id in (primary_id, followup_id):
            job = queue.fetch_job(job_id)
            if job is not None:
                try: job.delete()
                except Exception: pass
        connection.delete(f"rq:job:{primary_id}:dependencies", f"rq:job:{followup_id}:dependencies")
        connection.close()


def test_due_work_heat_marker_waits_for_native_rq_promotion(monkeypatch):
    from app.services import work_heat_queue as module
    from app.services.work_heat_queue import defer_work_heat_for_resources, promote_work_heat_resource_waits

    connection = _redis()
    queue, registry, job, first_seen = _claimed_job(connection)
    try:
        assert defer_work_heat_for_resources(job, queue, reason="profile_busy", delay_seconds=60)
        connection.zadd(registry.key, {job.id: time() - 1})
        due_score = float(connection.zscore(registry.key, job.id))
        monkeypatch.setattr(module, "_work_heat_resource_wait_ready", lambda _connection: (_ for _ in ()).throw(AssertionError("ready probe must not run")))
        assert promote_work_heat_resource_waits(connection, [registry], now=time()) == 0
        assert float(connection.zscore(registry.key, job.id)) == due_score
    finally:
        _cleanup(connection, registry, job, first_seen)

def test_scheduled_work_heat_without_marker_is_not_promoted(monkeypatch):
    from rq import Queue
    from rq.registry import ScheduledJobRegistry
    from app.services import work_heat_queue as module
    from app.services.work_heat_queue import promote_work_heat_resource_waits

    connection = _redis()
    queue = Queue("maintenance", connection=connection)
    registry = ScheduledJobRegistry(queue=queue)
    job = queue.enqueue_at(
        datetime.now(timezone.utc) + timedelta(minutes=5),
        "app.jobs.work_heat.run_work_heat_recompute",
        "pixiv", job_id=f"t-work-heat-ordinary-{uuid4().hex}",
    )
    try:
        monkeypatch.setattr(module, "_work_heat_resource_wait_ready", lambda _connection: True)
        assert promote_work_heat_resource_waits(connection, [registry], now=time() + 60) == 0
    finally:
        _cleanup(connection, registry, job, queue.intermediate_queue.get_first_seen_key(job.id))


def test_old_claim_cannot_park_recreated_queued_generation():
    from rq import Queue
    from app.services.work_heat_queue import WORK_HEAT_RESOURCE_WAIT_MARKER, defer_work_heat_for_resources

    connection = _redis()
    queue, registry, old_job, first_seen = _claimed_job(connection)
    new_job = None
    try:
        connection.lrem(queue.intermediate_queue.key, 0, old_job.id)
        old_job.delete()
        new_job = queue.enqueue("app.jobs.work_heat.run_work_heat_recompute", "manual", job_id=old_job.id)
        new_job, _ = Queue.dequeue_any([queue], timeout=1, connection=connection)
        assert new_job.created_at != old_job.created_at
        assert defer_work_heat_for_resources(old_job, queue, reason="profile_busy") is False
        assert new_job.get_status(refresh=True).value == "queued"
        assert connection.lpos(queue.intermediate_queue.key, new_job.id) is not None
        assert connection.zscore(registry.key, new_job.id) is None
        assert connection.hget(new_job.key, WORK_HEAT_RESOURCE_WAIT_MARKER) is None
    finally:
        connection.lrem(queue.intermediate_queue.key, 0, old_job.id)
        _cleanup(connection, registry, new_job or old_job, first_seen)
