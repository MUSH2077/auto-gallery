"""Capacity gates for download producers.

Manual work may be queued while host pressure is paused, but no producer may
bypass storage, Redis writability, Redis capacity, or the bounded queue.  The
automatic scheduler additionally stops producing while host pressure is high.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.storage_artifact import StorageArtifact
from app.services.queue_admission import (
    REDIS_ENQUEUE_STOP_RATIO,
    QueueAdmissionError,
    ensure_redis_enqueue_capacity,
)
from app.services.redis_client import get_redis

DOWNLOAD_QUEUE_NAMES = (
    "downloads",
    "downloads:pixiv",
    "downloads:danbooru",
    "downloads:iwara",
    "downloads:weibo",
    "downloads:bilibili",
    "downloads:pinterest",
    "downloads:lofter",
    "downloads:x",
)
DEFAULT_MAX_QUEUED_DOWNLOADS = 100
REDIS_WARN_RATIO = 0.80
REDIS_STOP_RATIO = REDIS_ENQUEUE_STOP_RATIO
DOWNLOAD_ENQUEUE_LOCK_KEY = "lock:download-enqueue-admission"
# The critical section performs aggregate registry reads plus one RQ MULTI/EXEC.
# Keep the lease comfortably above Redis' 15s socket timeout/retry window so a
# degraded Redis cannot expire the lock while the first producer is still in it.
DOWNLOAD_ENQUEUE_LOCK_SECONDS = 120
DOWNLOAD_ENQUEUE_LOCK_WAIT_SECONDS = 5
RESOURCE_WORK_CHANNEL_PREFIX = "resource:work:"

logger = logging.getLogger(__name__)


class DownloadAdmissionError(RuntimeError):
    """Structured error for HTTP/RQ download admission failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 503,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


@dataclass
class DownloadAdmissionSnapshot:
    """One scheduler-cycle capacity snapshot; hard enqueue checks still win."""

    reason: dict[str, Any] | None
    queued: int | None
    maximum_queued: int
    remaining_slots: int
    automatic: bool
    throughput_scale: float = 1.0
    computed_throughput_scale: float = 1.0
    controller_mode: str | None = None
    governance_mode: str | None = None

    def reserve_published_slot(self) -> None:
        self.remaining_slots = max(0, self.remaining_slots - 1)


_download_admission_snapshot: ContextVar[DownloadAdmissionSnapshot | None] = ContextVar(
    "download_admission_snapshot",
    default=None,
)


def _download_queue_limit() -> int:
    try:
        return max(1, int(settings.download_queue_max_pending))
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUEUED_DOWNLOADS


def _redis_capacity_reason(redis) -> dict[str, Any] | None:
    try:
        ensure_redis_enqueue_capacity(redis)
    except QueueAdmissionError as exc:
        return exc.payload()
    return None


def _download_waiting_count(redis, *, extra_queue_name: str | None = None) -> int:
    """Count queued, intermediate, scheduled and deferred download jobs.

    Started/finished/failed registries are intentionally excluded: the hard
    ceiling protects Redis-backed waiting work, not the single running worker.
    RQ's intermediate list closes the LMOVE-to-StartedRegistry crash window.
    Queue names are the explicit routing contract, avoiding ``Queue.all()``'s
    Redis SCAN on every admission.  All list/zset counts share one pipeline.
    """

    from rq import Queue
    from rq.registry import DeferredJobRegistry, ScheduledJobRegistry

    queue_names = set(DOWNLOAD_QUEUE_NAMES)
    if extra_queue_name:
        queue_names.add(extra_queue_name)
    queue_contracts = []
    for name in sorted(queue_names):
        queue = Queue(name=name, connection=redis)
        scheduled = ScheduledJobRegistry(name=name, connection=redis)
        deferred = DeferredJobRegistry(name=name, connection=redis)
        queue_contracts.append((queue, scheduled, deferred))

    pipeline_factory = getattr(redis, "pipeline", None)
    if not callable(pipeline_factory):
        # Small compatibility path for tests and old Redis proxies.  Production
        # redis-py always supplies a pipeline.
        return sum(
            int(queue.count)
            + int(redis.llen(queue.intermediate_queue_key))
            + int(scheduled.count)
            + int(deferred.count)
            for queue, scheduled, deferred in queue_contracts
        )

    pipeline = pipeline_factory(transaction=False)
    for queue, scheduled, deferred in queue_contracts:
        pipeline.llen(queue.key)
        pipeline.llen(queue.intermediate_queue_key)
        pipeline.zcard(scheduled.key)
        pipeline.zcard(deferred.key)
    return sum(int(value or 0) for value in pipeline.execute())


def _redis_capacity_and_queue_state(
    redis=None,
    *,
    extra_queue_name: str | None = None,
) -> tuple[dict[str, Any] | None, int | None, int]:
    redis = redis or get_redis()
    maximum_queued = _download_queue_limit()
    try:
        capacity_reason = _redis_capacity_reason(redis)
        if capacity_reason:
            return capacity_reason, None, maximum_queued

        waiting = _download_waiting_count(redis, extra_queue_name=extra_queue_name)
        if waiting >= maximum_queued:
            return {
                "code": "queue_saturated",
                "message": "Download queue has reached its waiting-job limit",
                "queued": waiting,
                "maximum_queued": maximum_queued,
            }, waiting, maximum_queued
        return None, waiting, maximum_queued
    except Exception as exc:
        return {
            "code": "redis_unwritable",
            "message": "Redis cannot accept download jobs",
            "error_type": type(exc).__name__,
        }, None, maximum_queued


def _redis_capacity_and_queue_reason(
    redis=None,
    *,
    extra_queue_name: str | None = None,
) -> dict[str, Any] | None:
    """Fail closed when Redis cannot safely accept another durable RQ job."""

    return _redis_capacity_and_queue_state(
        redis,
        extra_queue_name=extra_queue_name,
    )[0]


def _existing_rq_job(redis, rq_job_id: str):
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        return Job.fetch(rq_job_id, connection=redis)
    except NoSuchJobError:
        return None


def _notify_download_worker(redis) -> None:
    try:
        redis.publish(f"{RESOURCE_WORK_CHANNEL_PREFIX}download", "queued")
    except Exception:
        logger.debug("Unable to publish download work event", exc_info=True)


def _consume_batch_slot() -> None:
    snapshot = _download_admission_snapshot.get()
    if snapshot is not None:
        snapshot.reserve_published_slot()


def enqueue_download_rq(
    queue_name: str,
    func_path: str,
    *args: Any,
    rq_job_id: str,
    job_timeout: int,
    delay_seconds: int | float | None = None,
    redis_client=None,
):
    """Atomically admit and publish one download job.

    Every download producer must use this function.  The Redis lock makes the
    aggregate ``count + enqueue`` operation serial across producers, so the
    waiting-job ceiling cannot be overshot by concurrent requests.  A durable,
    deterministic RQ id makes retries safe when a Redis response is lost after
    the enqueue was accepted.
    """

    redis = redis_client or get_redis()
    lock = redis.lock(
        DOWNLOAD_ENQUEUE_LOCK_KEY,
        timeout=DOWNLOAD_ENQUEUE_LOCK_SECONDS,
        blocking_timeout=DOWNLOAD_ENQUEUE_LOCK_WAIT_SECONDS,
    )
    try:
        acquired = lock.acquire(blocking=True)
    except Exception as exc:
        raise DownloadAdmissionError(
            "redis_unwritable",
            "Redis download admission lock is unavailable",
            details={"error_type": type(exc).__name__},
        ) from exc
    if not acquired:
        raise DownloadAdmissionError(
            "enqueue_busy",
            "Download admission is busy; retry shortly",
            details={"lock_wait_seconds": DOWNLOAD_ENQUEUE_LOCK_WAIT_SECONDS},
        )

    try:
        # An earlier request may have succeeded but lost its Redis response.
        # Treat the durable id as proof of publication before applying current
        # capacity gates, otherwise an idempotent retry could be rejected.
        existing = _existing_rq_job(redis, rq_job_id)
        if existing is not None:
            return existing

        reason = _redis_capacity_and_queue_reason(
            redis,
            extra_queue_name=queue_name,
        )
        if reason:
            raise admission_error(reason)

        from rq import Queue

        queue = Queue(name=queue_name, connection=redis)
        try:
            if delay_seconds is not None and delay_seconds > 0:
                rq_job = queue.enqueue_in(
                    timedelta(seconds=delay_seconds),
                    func_path,
                    *args,
                    job_id=rq_job_id,
                    job_timeout=job_timeout,
                )
            else:
                rq_job = queue.enqueue(
                    func_path,
                    *args,
                    job_id=rq_job_id,
                    job_timeout=job_timeout,
                )
        except Exception as exc:
            # Resolve the ambiguous-response case: Redis may have committed the
            # job even though the client observed a timeout/reset.
            try:
                existing = _existing_rq_job(redis, rq_job_id)
            except Exception:
                existing = None
            if existing is not None:
                logger.warning(
                    "RQ enqueue response was ambiguous; recovered download job %s",
                    rq_job_id,
                )
                _consume_batch_slot()
                _notify_download_worker(redis)
                return existing
            raise DownloadAdmissionError(
                "redis_unwritable",
                "Redis rejected the download job",
                details={"error_type": type(exc).__name__, "rq_job_id": rq_job_id},
            ) from exc
        _consume_batch_slot()
        _notify_download_worker(redis)
        return rq_job
    finally:
        try:
            lock.release()
        except Exception:
            # The admission outcome is already known.  A lease-expiry race must
            # not turn a successfully published deterministic job into failure.
            logger.warning("Unable to release download admission lock", exc_info=True)


async def download_queue_reason() -> dict[str, Any] | None:
    return await asyncio.to_thread(_redis_capacity_and_queue_reason)


async def _base_download_backpressure_reason(
    db: AsyncSession,
    *,
    automatic: bool = False,
) -> dict[str, Any] | None:
    try:
        usage = await asyncio.to_thread(shutil.disk_usage, settings.download_root)
    except Exception as exc:
        return {
            "code": "storage_unavailable",
            "message": "Download storage is unavailable",
            "error_type": type(exc).__name__,
        }
    free_gb = usage.free / (1024 ** 3)
    if free_gb < settings.min_download_free_gb:
        return {
            "code": "disk_backpressure",
            "message": "Download storage is below the free-space floor",
            "free_gb": round(free_gb, 2),
            "minimum_free_gb": settings.min_download_free_gb,
        }

    pending = (
        await db.execute(
            select(func.count(StorageArtifact.id)).where(
                StorageArtifact.storage_root == "downloads",
                StorageArtifact.artifact_type == "metadata_json",
                StorageArtifact.state.in_(("new", "importing")),
            )
        )
    ).scalar_one()
    if pending >= settings.max_pending_artifacts:
        return {
            "code": "import_backpressure",
            "message": "Pending imports have reached the configured limit",
            "pending": pending,
            "maximum_pending": settings.max_pending_artifacts,
        }

    if automatic:
        from app.services.resource_pressure import (
            get_resource_pressure_snapshot,
        )

        snapshot = await get_resource_pressure_snapshot()
        return _automatic_download_pressure_reason(snapshot)
    return None


def _automatic_download_pressure_reason(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Translate the shared controller snapshot into the producer hard gate."""

    from app.services.resource_pressure import resource_profile_permit

    permitted, profile = resource_profile_permit(snapshot, "download")
    if permitted:
        return None
    budget = snapshot.get("budget") or {}
    return {
        "code": "resource_pressure",
        "message": "Automatic downloads are paused to protect the NAS",
        "pressure_status": snapshot.get("status"),
        "controller_mode": snapshot.get("controller_mode"),
        "throughput_scale": budget.get(
            "effective_throughput_scale",
            budget.get("throughput_scale"),
        ),
        "computed_throughput_scale": budget.get("computed_throughput_scale"),
        "profile": profile,
        "reasons": snapshot.get("reasons") or [],
    }


def _pressure_scales(snapshot: dict[str, Any] | None) -> tuple[float, float, str | None, str | None]:
    """Read rolling-upgrade-safe effective/computed controller scales."""

    snapshot = snapshot or {}
    budget = snapshot.get("budget") or {}

    def bounded(value: Any, fallback: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return fallback

    effective = bounded(
        budget.get("effective_throughput_scale", budget.get("throughput_scale")),
        0.0 if snapshot.get("status") == "paused" else 1.0,
    )
    computed = bounded(budget.get("computed_throughput_scale"), effective)
    return (
        effective,
        computed,
        snapshot.get("controller_mode"),
        budget.get("governance_mode"),
    )


async def build_download_admission_snapshot(
    db: AsyncSession,
    *,
    automatic: bool = False,
    include_queue: bool = True,
) -> DownloadAdmissionSnapshot:
    """Build one expensive capacity snapshot for a scheduler producer batch."""

    # Keep the expensive disk/artifact query and controller read to one each per
    # scheduler cycle.  Manual producers retain their full rate; their jobs are
    # still stopped by the worker's hard profile gate before execution.
    reason = await _base_download_backpressure_reason(db, automatic=False)
    pressure_snapshot: dict[str, Any] | None = None
    if reason is None and automatic:
        from app.services.resource_pressure import get_resource_pressure_snapshot

        pressure_snapshot = await get_resource_pressure_snapshot()
        reason = _automatic_download_pressure_reason(pressure_snapshot)
    maximum = _download_queue_limit()
    queued: int | None = None
    if reason is None and include_queue:
        reason, queued, maximum = await asyncio.to_thread(
            _redis_capacity_and_queue_state
        )
    remaining = max(0, maximum - queued) if queued is not None and reason is None else 0
    effective_scale, computed_scale, controller_mode, governance_mode = _pressure_scales(
        pressure_snapshot
    )
    return DownloadAdmissionSnapshot(
        reason=reason,
        queued=queued,
        maximum_queued=maximum,
        remaining_slots=remaining,
        automatic=automatic,
        throughput_scale=effective_scale if automatic else 1.0,
        computed_throughput_scale=computed_scale if automatic else 1.0,
        controller_mode=controller_mode,
        governance_mode=governance_mode,
    )


@asynccontextmanager
async def download_admission_batch(
    db: AsyncSession,
    *,
    automatic: bool = True,
):
    """Cache disk/artifact/pressure/queue preflight for one producer cycle."""

    snapshot = await build_download_admission_snapshot(
        db,
        automatic=automatic,
        include_queue=True,
    )
    token = _download_admission_snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _download_admission_snapshot.reset(token)


async def download_backpressure_reason(
    db: AsyncSession,
    *,
    automatic: bool = False,
    include_queue: bool = False,
) -> dict[str, Any] | None:
    """Return the first hard reason, reusing a scheduler batch snapshot.

    The enqueue function still serializes and recounts under its Redis lock, so
    this cache removes producer N+1 work without weakening the hard queue cap.
    """

    cached = _download_admission_snapshot.get()
    if cached is not None and cached.automatic == automatic:
        if cached.reason:
            return dict(cached.reason)
        if include_queue and cached.remaining_slots <= 0:
            return {
                "code": "queue_saturated",
                "message": "Download queue has reached its waiting-job limit",
                "queued": cached.maximum_queued,
                "maximum_queued": cached.maximum_queued,
            }
        return None

    reason = await _base_download_backpressure_reason(db, automatic=automatic)
    if reason:
        return reason

    if include_queue:
        return await download_queue_reason()
    return None


def admission_error(reason: dict[str, Any]) -> DownloadAdmissionError:
    code = str(reason.get("code") or "download_unavailable")
    status_code = 429 if code == "queue_saturated" else 503
    message = str(reason.get("message") or code.replace("_", " "))
    return DownloadAdmissionError(
        code,
        message,
        status_code=status_code,
        details={key: value for key, value in reason.items() if key not in {"code", "message"}},
    )
