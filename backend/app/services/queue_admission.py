"""Shared Redis capacity and writability gates for every RQ producer.

Redis stores both queues and cache data in this deployment and deliberately uses
``noeviction``.  A successful PING therefore says nothing about whether RQ can
publish a job.  Producers call the helpers in this module immediately before an
enqueue so the application stops adding work at 90% of ``maxmemory`` and turns
write failures into one stable, observable error contract.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

import redis as redis_lib

from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

REDIS_ENQUEUE_WARN_RATIO = 0.80
REDIS_ENQUEUE_STOP_RATIO = 0.90
QUEUE_REJECTION_COUNTER_KEY = "resource:redis:enqueue_rejections"


class QueueAdmissionError(RuntimeError):
    """A queue publication was rejected before or by Redis."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


def _record_rejection(redis_client, *, code: str) -> None:
    """Best-effort application rejection counter used by system health.

    A proactive 90% rejection never reaches Redis' ``errorstat_OOM`` counter.
    Keep a separate tiny key so operators can distinguish the guard doing its
    job from a Redis server that has already started rejecting commands.
    """

    try:
        redis_client.incr(QUEUE_REJECTION_COUNTER_KEY)
    except Exception:
        # Never replace the original, structured admission error with a metrics
        # write failure.  The warning still leaves evidence when Redis is fully
        # saturated and cannot update the counter either.
        logger.warning(
            "Unable to record Redis queue rejection code=%s",
            code,
            exc_info=True,
        )


def ensure_redis_enqueue_capacity(redis_client=None) -> dict[str, int | float]:
    """Verify Redis is below 90% and can complete a short SET EX/DEL probe.

    Returns the sampled capacity values on success.  All failures use
    :class:`QueueAdmissionError`, allowing HTTP callers and workers to share the
    same behaviour without importing FastAPI into the service layer.
    """

    redis_client = redis_client or get_redis()
    try:
        memory = redis_client.info("memory")
        used = int(memory.get("used_memory") or 0)
        maximum = int(memory.get("maxmemory") or 0)
        ratio = used / maximum if maximum > 0 else 0.0
    except (redis_lib.RedisError, OSError, TypeError, ValueError) as exc:
        _record_rejection(redis_client, code="redis_unwritable")
        raise QueueAdmissionError(
            "redis_unwritable",
            "Redis capacity cannot be verified before enqueue",
            details={"error_type": type(exc).__name__},
        ) from exc

    if maximum > 0 and ratio >= REDIS_ENQUEUE_STOP_RATIO:
        _record_rejection(redis_client, code="redis_capacity")
        raise QueueAdmissionError(
            "redis_capacity",
            "Redis is at or above the safe enqueue limit",
            details={
                "used_bytes": used,
                "max_bytes": maximum,
                "usage_ratio": round(ratio, 4),
                "stop_ratio": REDIS_ENQUEUE_STOP_RATIO,
            },
        )

    probe_key = f"health:enqueue-write:{uuid.uuid4().hex}"
    try:
        wrote = redis_client.set(probe_key, b"1", ex=5)
        if not wrote:
            raise RuntimeError("Redis SET probe returned a false result")
        deleted = redis_client.delete(probe_key)
        if not deleted:
            raise RuntimeError("Redis DEL probe did not remove its key")
    except (redis_lib.RedisError, OSError, RuntimeError) as exc:
        _record_rejection(redis_client, code="redis_unwritable")
        raise QueueAdmissionError(
            "redis_unwritable",
            "Redis cannot accept queued jobs",
            details={"error_type": type(exc).__name__},
        ) from exc

    return {
        "used_bytes": used,
        "max_bytes": maximum,
        "usage_ratio": ratio,
    }


def _checked_queue_write(queue, operation: Callable[[], Any]) -> Any:
    redis_client = queue.connection
    ensure_redis_enqueue_capacity(redis_client)
    try:
        return operation()
    except QueueAdmissionError:
        raise
    except (redis_lib.RedisError, OSError) as exc:
        _record_rejection(redis_client, code="redis_unwritable")
        raise QueueAdmissionError(
            "redis_unwritable",
            "Redis rejected the queued job",
            details={
                "error_type": type(exc).__name__,
                "queue": str(getattr(queue, "name", "unknown")),
            },
        ) from exc


def checked_enqueue(queue, *args: Any, **kwargs: Any):
    """Capacity-check and call ``Queue.enqueue`` with structured failures."""

    return _checked_queue_write(queue, lambda: queue.enqueue(*args, **kwargs))


def checked_enqueue_in(queue, delay, *args: Any, **kwargs: Any):
    """Capacity-check and call ``Queue.enqueue_in`` with structured failures."""

    return _checked_queue_write(
        queue,
        lambda: queue.enqueue_in(delay, *args, **kwargs),
    )
