"""Durable scheduling loop for the subscription scanner.

The scanner is intentionally self-scheduling, but a worker crash must not be
able to break that chain.  This module is the single authority used by boot,
scan completion, and the worker supervisor watchdog.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import ScheduledJobRegistry, StartedJobRegistry

from app.services.queue_admission import checked_enqueue_in
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

SCHEDULER_QUEUE = "scheduled"
SCHEDULER_JOB_PREFIX = "subscription-sync-"
SCHEDULER_STATE_KEY = "scheduler:subscription-sync:loop:v1"
SCHEDULER_ENSURE_LOCK_KEY = "lock:scheduler:subscription-sync:ensure"
SCHEDULER_ENSURE_LOCK_TTL_SECONDS = 30
DEFAULT_SCAN_INTERVAL_MINUTES = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _decode(value) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _parse_datetime(value) -> datetime | None:
    raw = _decode(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def subscription_sync_job_id(scan_interval_minutes: int, target_at: datetime) -> str:
    """Return an RQ-safe deterministic ID for one future scan slot."""

    slot_seconds = max(5, int(scan_interval_minutes)) * 60
    slot = int(target_at.timestamp() // slot_seconds)
    return f"{SCHEDULER_JOB_PREFIX}{slot_seconds}-{slot}"


def _job_is_subscription_sync(job) -> bool:
    return bool(job) and "sync_subscriptions" in (
        getattr(job, "func_name", "") or str(job)
    )


def _active_sync_jobs(redis_client=None, *, exclude_job_id: str | None = None) -> dict[str, list]:
    redis_client = redis_client or get_redis()
    queue = Queue(name=SCHEDULER_QUEUE, connection=redis_client)
    registries = {
        "queued": queue.get_job_ids(),
        "scheduled": ScheduledJobRegistry(queue=queue).get_job_ids(),
        "started": StartedJobRegistry(queue=queue).get_job_ids(),
    }
    active: dict[str, list] = {name: [] for name in registries}
    for registry_name, job_ids in registries.items():
        for job_id in job_ids:
            normalized_id = _decode(job_id)
            if not normalized_id or normalized_id == exclude_job_id:
                continue
            try:
                job = Job.fetch(normalized_id, connection=redis_client)
            except NoSuchJobError:
                continue
            if _job_is_subscription_sync(job):
                active[registry_name].append(job)
    return active


def _write_state(redis_client, **values) -> None:
    payload = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in values.items()
        if value is not None
    }
    if payload:
        redis_client.hset(SCHEDULER_STATE_KEY, mapping=payload)


def mark_scheduler_scan_started(*, scan_interval_minutes: int | None = None) -> None:
    try:
        redis_client = get_redis()
        _write_state(
            redis_client,
            last_started_at=_now(),
            scan_interval_minutes=max(5, int(scan_interval_minutes or DEFAULT_SCAN_INTERVAL_MINUTES)),
            last_error="",
        )
    except Exception:
        logger.warning("Unable to persist scheduler start state", exc_info=True)


def mark_scheduler_scan_finished(
    *,
    scan_interval_minutes: int,
    next_scan_at: datetime | None,
) -> None:
    try:
        _write_state(
            get_redis(),
            last_finished_at=_now(),
            scan_interval_minutes=max(5, int(scan_interval_minutes)),
            next_scan_at=next_scan_at,
            last_error="",
        )
    except Exception:
        logger.warning("Unable to persist scheduler finish state", exc_info=True)


def mark_scheduler_scan_error(error: Exception | str, *, scan_interval_minutes: int | None = None) -> None:
    try:
        _write_state(
            get_redis(),
            last_error_at=_now(),
            last_error=str(error)[:1000],
            scan_interval_minutes=max(5, int(scan_interval_minutes or DEFAULT_SCAN_INTERVAL_MINUTES)),
        )
    except Exception:
        logger.warning("Unable to persist scheduler error state", exc_info=True)


def _release_lock(redis_client, token: str) -> None:
    redis_client.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('del', KEYS[1]) else return 0 end",
        1,
        SCHEDULER_ENSURE_LOCK_KEY,
        token,
    )


def ensure_next_subscription_scan(
    scan_interval_minutes: int = DEFAULT_SCAN_INTERVAL_MINUTES,
    *,
    redis_client=None,
    exclude_job_id: str | None = None,
    watchdog: bool = False,
) -> dict:
    """Ensure exactly one queued, scheduled, or running scanner exists."""

    redis_client = redis_client or get_redis()
    interval = max(5, int(scan_interval_minutes))
    now = _now()
    if watchdog:
        _write_state(redis_client, watchdog_at=now, scan_interval_minutes=interval)

    active = _active_sync_jobs(redis_client, exclude_job_id=exclude_job_id)
    existing = next(
        (job for name in ("started", "queued", "scheduled") for job in active[name]),
        None,
    )
    if existing is not None:
        snapshot = scheduler_loop_snapshot(redis_client=redis_client, scan_interval_minutes=interval)
        return {"created": False, "job_id": existing.id, **snapshot}

    token = secrets.token_hex(16)
    acquired = bool(
        redis_client.set(
            SCHEDULER_ENSURE_LOCK_KEY,
            token,
            nx=True,
            ex=SCHEDULER_ENSURE_LOCK_TTL_SECONDS,
        )
    )
    if not acquired:
        return {"created": False, "locked": True, **scheduler_loop_snapshot(redis_client=redis_client, scan_interval_minutes=interval)}

    try:
        active = _active_sync_jobs(redis_client, exclude_job_id=exclude_job_id)
        existing = next(
            (job for name in ("started", "queued", "scheduled") for job in active[name]),
            None,
        )
        if existing is not None:
            return {"created": False, "job_id": existing.id, **scheduler_loop_snapshot(redis_client=redis_client, scan_interval_minutes=interval)}

        from app.jobs.subscription_sync import sync_subscriptions

        target_at = now + timedelta(minutes=interval)
        job_id = subscription_sync_job_id(interval, target_at)
        try:
            old_job = Job.fetch(job_id, connection=redis_client)
        except NoSuchJobError:
            old_job = None
        if old_job is not None:
            old_job.delete()

        queue = Queue(name=SCHEDULER_QUEUE, connection=redis_client)
        checked_enqueue_in(
            queue,
            timedelta(minutes=interval),
            sync_subscriptions,
            job_id=job_id,
        )
        _write_state(
            redis_client,
            next_scan_at=target_at,
            scan_interval_minutes=interval,
        )
        return {
            "created": True,
            "job_id": job_id,
            "next_scan_at": target_at.isoformat(),
            "status": "scheduled",
        }
    finally:
        try:
            _release_lock(redis_client, token)
        except Exception:
            logger.warning("Unable to release scheduler ensure lock", exc_info=True)


def scheduler_loop_snapshot(
    *,
    redis_client=None,
    scan_interval_minutes: int | None = None,
) -> dict:
    redis_client = redis_client or get_redis()
    raw = redis_client.hgetall(SCHEDULER_STATE_KEY)
    state = {_decode(key): _decode(value) for key, value in raw.items()}
    interval = max(
        5,
        int(scan_interval_minutes or state.get("scan_interval_minutes") or DEFAULT_SCAN_INTERVAL_MINUTES),
    )
    active = _active_sync_jobs(redis_client)
    started = bool(active["started"])
    queued = bool(active["queued"])
    scheduled = bool(active["scheduled"])

    next_scan_at = _parse_datetime(state.get("next_scan_at"))
    if active["scheduled"]:
        registry = ScheduledJobRegistry(
            queue=Queue(name=SCHEDULER_QUEUE, connection=redis_client)
        )
        scores = [
            redis_client.zscore(registry.key, job.id)
            for job in active["scheduled"]
        ]
        valid_scores = [float(score) for score in scores if score is not None]
        if valid_scores:
            next_scan_at = datetime.fromtimestamp(min(valid_scores), tz=timezone.utc)

    last_started = _parse_datetime(state.get("last_started_at"))
    last_finished = _parse_datetime(state.get("last_finished_at"))
    last_error_at = _parse_datetime(state.get("last_error_at"))
    watchdog_at = _parse_datetime(state.get("watchdog_at"))
    last_activity = max(
        (value for value in (last_started, last_finished, last_error_at) if value),
        default=None,
    )
    stalled = not (started or queued or scheduled) and (
        last_activity is None
        or (_now() - last_activity) >= timedelta(minutes=interval * 2)
    )
    status = (
        "stalled"
        if stalled
        else "running"
        if started
        else "scheduled"
        if scheduled or queued
        else "recovering"
    )
    return {
        "status": status,
        "last_started_at": _iso(last_started),
        "last_finished_at": _iso(last_finished),
        "next_scan_at": _iso(next_scan_at),
        "watchdog_at": _iso(watchdog_at),
        "last_error": state.get("last_error") or None,
        "scan_interval_minutes": interval,
        "active": {
            "queued": len(active["queued"]),
            "scheduled": len(active["scheduled"]),
            "started": len(active["started"]),
        },
    }


def scheduler_watchdog() -> dict:
    """Supervisor entry point; safe to call once a minute forever."""

    redis_client = get_redis()
    state = redis_client.hgetall(SCHEDULER_STATE_KEY)
    interval_raw = state.get(b"scan_interval_minutes") or state.get("scan_interval_minutes")
    try:
        interval = max(5, int(_decode(interval_raw) or DEFAULT_SCAN_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        interval = DEFAULT_SCAN_INTERVAL_MINUTES
    return ensure_next_subscription_scan(
        interval,
        redis_client=redis_client,
        watchdog=True,
    )
