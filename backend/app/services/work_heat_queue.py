"""Coalesced, non-blocking requests for source heat recomputation."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job

from app.services.queue_admission import checked_enqueue
from app.services.redis_client import get_redis


logger = logging.getLogger(__name__)
_SOURCE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,48}\Z")
_PENDING_STATUSES = frozenset({"queued", "scheduled", "deferred"})
_RUNNING_STATUSES = frozenset({"started", "busy"})


def _status(job) -> str:
    value = job.get_status(refresh=True)
    return str(getattr(value, "value", value)).casefold()


def _fetch(job_id: str, redis_client):
    try:
        return Job.fetch(job_id, connection=redis_client)
    except NoSuchJobError:
        return None


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
            primary_id = f"work-heat-{source}"
            primary = _fetch(primary_id, redis_client)
            primary_status = _status(primary) if primary is not None else None
            if primary_status in _PENDING_STATUSES:
                outcome["coalesced"] += 1
                continue

            job_id = primary_id
            if primary_status in _RUNNING_STATUSES:
                job_id = f"{primary_id}-followup"
                followup = _fetch(job_id, redis_client)
                if followup is not None and _status(followup) in (
                    _PENDING_STATUSES | _RUNNING_STATUSES
                ):
                    outcome["coalesced"] += 1
                    continue
                if followup is not None:
                    followup.delete()
            elif primary is not None:
                primary.delete()

            checked_enqueue(
                queue,
                run_work_heat_recompute,
                source,
                job_id=job_id,
                job_timeout=3600,
                result_ttl=60 * 60,
                failure_ttl=24 * 60 * 60,
                retry=Retry(max=3, interval=[60, 300, 900]),
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


__all__ = ["request_work_heat_recompute"]
