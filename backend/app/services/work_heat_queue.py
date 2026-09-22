"""Coalesced, non-blocking requests for source heat recomputation."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from contextlib import nullcontext
from threading import Event, Thread

from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Dependency, Job
from rq.registry import DeferredJobRegistry
from redis.exceptions import LockNotOwnedError

from app.services.queue_admission import checked_enqueue, notify_queue_worker
from app.services.redis_client import get_redis


logger = logging.getLogger(__name__)
_SOURCE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,48}\Z")
_PENDING_STATUSES = frozenset({"queued", "scheduled", "deferred"})
_RUNNING_STATUSES = frozenset({"started", "busy"})
_DEPENDENCY_RELEASE_STATUSES = frozenset({"finished", "failed"})
# Redis commands use a 15-second socket timeout with one retry. Keep the lease
# well above that worst single-command window and renew it while the enqueue
# transaction is alive; process death still releases the lease within two minutes.
_ENQUEUE_LOCK_TIMEOUT_SECONDS = 120
_ENQUEUE_LOCK_BLOCKING_SECONDS = 10
_ENQUEUE_LOCK_RENEW_INTERVAL_SECONDS = 20


def _status(job) -> str:
    value = job.get_status(refresh=True)
    return str(getattr(value, "value", value)).casefold()


def _fetch(job_id: str, redis_client):
    try:
        return Job.fetch(job_id, connection=redis_client)
    except NoSuchJobError:
        return None


class _RenewingSourceLock:
    def __init__(self, lock, *, timeout: float, renew_interval: float):
        self._lock = lock
        self._timeout = timeout
        self._renew_interval = renew_interval
        self._stop = Event()
        self._lost = Event()
        self._thread: Thread | None = None

    def __enter__(self):
        if not self._lock.acquire():
            raise TimeoutError("Timed out acquiring work heat enqueue lock")
        self._thread = Thread(
            target=self._renew,
            name="work-heat-enqueue-lock-renewal",
            daemon=True,
        )
        self._thread.start()
        return self

    def _renew(self) -> None:
        while not self._stop.wait(self._renew_interval):
            try:
                self._lock.extend(self._timeout, replace_ttl=True)
            except Exception:
                self._lost.set()
                return

    def ensure_owned(self) -> None:
        if self._lost.is_set() or not self._lock.owned():
            raise RuntimeError("Work heat enqueue lock ownership was lost")

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        try:
            self._lock.release()
        except LockNotOwnedError:
            if exc_type is None:
                raise RuntimeError("Work heat enqueue lock expired before release")
        return False


def _source_enqueue_lock(redis_client, source: str):
    lock_factory = getattr(redis_client, "lock", None)
    if not callable(lock_factory):
        # Lightweight unit doubles do not need to implement redis-py's lock API.
        # Production callers always receive the real Redis client.
        return nullcontext()
    return _RenewingSourceLock(
        lock_factory(
            f"lock:work-heat:enqueue:{source}",
            timeout=_ENQUEUE_LOCK_TIMEOUT_SECONDS,
            blocking_timeout=_ENQUEUE_LOCK_BLOCKING_SECONDS,
            thread_local=False,
        ),
        timeout=_ENQUEUE_LOCK_TIMEOUT_SECONDS,
        renew_interval=min(
            _ENQUEUE_LOCK_RENEW_INTERVAL_SECONDS,
            _ENQUEUE_LOCK_TIMEOUT_SECONDS / 3,
        ),
    )


def _ensure_enqueue_lock_owned(lock_guard) -> None:
    ensure_owned = getattr(lock_guard, "ensure_owned", None)
    if callable(ensure_owned):
        ensure_owned()


def _enqueue_recompute_job(
    queue,
    function,
    source: str,
    job_id: str,
    *,
    dependency_id: str | None = None,
):
    options = {
        "job_id": job_id,
        "job_timeout": 3600,
        "result_ttl": 60 * 60,
        "failure_ttl": 24 * 60 * 60,
        "retry": Retry(max=3, interval=[60, 300, 900]),
    }
    if dependency_id is not None:
        options["depends_on"] = Dependency(
            jobs=dependency_id,
            allow_failure=True,
        )
    return checked_enqueue(queue, function, source, **options)


def _repair_deferred_dependency(
    queue,
    job,
    *,
    parent_id: str,
    redis_client,
    lock_guard=None,
) -> str | None:
    """Close the RQ race where a dependency terminates during registration."""

    if job is None:
        return None
    status = _status(job)
    if status != "deferred":
        return status

    parent = _fetch(parent_id, redis_client)
    parent_status = _status(parent) if parent is not None else None
    if parent_status in (_PENDING_STATUSES | _RUNNING_STATUSES):
        return status

    if parent is not None and parent_status in _DEPENDENCY_RELEASE_STATUSES:
        # RQ normally performs this transition when the parent finishes.  If
        # registration lost that event, replaying it is guarded by WATCH and is
        # safe even when the worker is concurrently doing the same thing.
        _ensure_enqueue_lock_owned(lock_guard)
        queue.enqueue_dependents(parent)
        notify_queue_worker(queue.name, redis_client, existing_job=job)
        status = _status(job)
        if status != "deferred":
            return status
        # Also recover a legacy/malformed deferred job that did not opt into
        # failed-parent release; a terminal parent cannot overlap the replay.

    # STOPPED/CANCELED parents never release dependents in RQ, and a missing
    # parent cannot emit a future completion event. This child already passed
    # admission, so promote it in one Redis transaction rather than deleting it
    # before a fallible replacement enqueue.
    return _promote_deferred_job_atomically(
        queue,
        job,
        parent_id=parent_id,
        redis_client=redis_client,
        lock_guard=lock_guard,
    )


def _promote_deferred_job_atomically(
    queue,
    job,
    *,
    parent_id: str,
    redis_client,
    lock_guard=None,
) -> str:
    """Detach and queue one accepted deferred job without a delete/enqueue gap."""

    pipeline = redis_client.pipeline(transaction=True)
    executed = False
    try:
        _ensure_enqueue_lock_owned(lock_guard)
        pipeline.srem(Job.dependents_key_for(parent_id), job.id)
        _ensure_enqueue_lock_owned(lock_guard)
        pipeline.delete(job.dependencies_key)
        _ensure_enqueue_lock_owned(lock_guard)
        # RQ uses the same private primitive from enqueue_dependents. Because
        # the job is still DEFERRED in memory, it also removes the registry row
        # in this transaction before persisting QUEUED and pushing the id.
        queue._enqueue_job(job, pipeline=pipeline)
        _ensure_enqueue_lock_owned(lock_guard)
        pipeline.execute()
        executed = True
    finally:
        if not executed:
            pipeline.reset()

    job._dependency_ids = []
    notify_queue_worker(queue.name, redis_client, existing_job=job)
    return _status(job)


def request_work_heat_recompute(
    sources: Iterable[str],
    *,
    redis_client=None,
) -> dict[str, int]:
    """Queue at most one pending recompute after every currently running job."""

    normalized = sorted(
        {
            source.strip().lower()
            for source in sources
            if isinstance(source, str) and _SOURCE_PATTERN.fullmatch(source.strip().lower())
        }
    )
    outcome = {"created": 0, "coalesced": 0, "errors": 0}
    if not normalized:
        return outcome

    redis_client = redis_client or get_redis()
    queue = Queue(name="maintenance", connection=redis_client)
    from app.jobs.work_heat import run_work_heat_recompute

    for source in normalized:
        try:
            with _source_enqueue_lock(redis_client, source) as lock_guard:
                _ensure_enqueue_lock_owned(lock_guard)
                primary_id = f"work-heat-{source}"
                followup_id = f"{primary_id}-followup"
                primary = _fetch(primary_id, redis_client)
                followup = _fetch(followup_id, redis_client)
                primary_status = _status(primary) if primary is not None else None
                followup_status = _status(followup) if followup is not None else None

                if primary_status == "deferred":
                    _ensure_enqueue_lock_owned(lock_guard)
                    primary_status = _repair_deferred_dependency(
                        queue,
                        primary,
                        parent_id=followup_id,
                        redis_client=redis_client,
                        lock_guard=lock_guard,
                    )
                if followup_status == "deferred":
                    _ensure_enqueue_lock_owned(lock_guard)
                    followup_status = _repair_deferred_dependency(
                        queue,
                        followup,
                        parent_id=primary_id,
                        redis_client=redis_client,
                        lock_guard=lock_guard,
                    )

                if primary_status in _PENDING_STATUSES:
                    outcome["coalesced"] += 1
                    continue

                job_id = primary_id
                dependency_id = None
                if primary_status in _RUNNING_STATUSES:
                    job_id = followup_id
                    if followup_status in (_PENDING_STATUSES | _RUNNING_STATUSES):
                        outcome["coalesced"] += 1
                        continue
                    if followup is not None:
                        _ensure_enqueue_lock_owned(lock_guard)
                        followup.delete()
                    # The maintenance queue may have multiple workers.  An explicit
                    # dependency keeps the one allowed follow-up from overlapping
                    # the primary recomputation it is meant to supersede.
                    dependency_id = primary_id
                else:
                    if followup_status in _PENDING_STATUSES:
                        outcome["coalesced"] += 1
                        continue
                    if followup_status in _RUNNING_STATUSES:
                        # A trigger that arrives after the follow-up started cannot
                        # be represented by that job's already-open transaction.
                        # Reuse the now-terminal primary id as a dependent successor
                        # so the new generation is never silently coalesced away.
                        dependency_id = followup_id
                        if primary is not None:
                            _ensure_enqueue_lock_owned(lock_guard)
                            primary.delete()
                    else:
                        if followup is not None:
                            _ensure_enqueue_lock_owned(lock_guard)
                            followup.delete()
                        if primary is not None:
                            _ensure_enqueue_lock_owned(lock_guard)
                            primary.delete()

                _ensure_enqueue_lock_owned(lock_guard)
                job = _enqueue_recompute_job(
                    queue,
                    run_work_heat_recompute,
                    source,
                    job_id,
                    dependency_id=dependency_id,
                )
                if dependency_id is not None and job is not None:
                    _repair_deferred_dependency(
                        queue,
                        job,
                        parent_id=dependency_id,
                        redis_client=redis_client,
                        lock_guard=lock_guard,
                    )
                outcome["created"] += 1
        except Exception:
            outcome["errors"] += 1
            logger.warning(
                "Unable to queue heat recomputation for source=%s",
                source,
                exc_info=True,
            )
    return outcome


def _work_heat_job_source(job) -> str | None:
    if not str(getattr(job, "func_name", "")).endswith("run_work_heat_recompute"):
        return None
    args = job.args
    if not args or not isinstance(args[0], str):
        return None
    source = args[0].strip().lower()
    return source if _SOURCE_PATTERN.fullmatch(source) else None


def reconcile_deferred_work_heat_jobs(*, redis_client=None) -> dict[str, int]:
    """Repair heat successors whose dependency emitted no usable terminal event."""

    redis_client = redis_client or get_redis()
    queue = Queue(name="maintenance", connection=redis_client)
    registry = DeferredJobRegistry(queue=queue)
    outcome = {"checked": 0, "recovered": 0, "waiting": 0, "errors": 0}

    for job_id in registry.get_job_ids():
        if not str(job_id).startswith("work-heat-"):
            continue
        try:
            job = _fetch(str(job_id), redis_client)
            source = _work_heat_job_source(job) if job is not None else None
            if source is None:
                continue
            primary_id = f"work-heat-{source}"
            followup_id = f"{primary_id}-followup"
            if job.id == primary_id:
                parent_id = followup_id
            elif job.id == followup_id:
                parent_id = primary_id
            else:
                continue

            with _source_enqueue_lock(redis_client, source) as lock_guard:
                _ensure_enqueue_lock_owned(lock_guard)
                current = _fetch(job.id, redis_client)
                if current is None or _status(current) != "deferred":
                    continue
                outcome["checked"] += 1
                status = _repair_deferred_dependency(
                    queue,
                    current,
                    parent_id=parent_id,
                    redis_client=redis_client,
                    lock_guard=lock_guard,
                )
                if status == "deferred":
                    outcome["waiting"] += 1
                else:
                    outcome["recovered"] += 1
        except Exception:
            outcome["errors"] += 1
            logger.warning(
                "Unable to reconcile deferred heat job id=%s",
                job_id,
                exc_info=True,
            )
    return outcome


__all__ = ["reconcile_deferred_work_heat_jobs", "request_work_heat_recompute"]
