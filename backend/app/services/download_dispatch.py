"""Durable, bounded publication of download jobs to RQ.

The database row and its TaskRun projection are committed before Redis can
make the work visible to a worker.  Redis publication then uses the single
atomic admission path in :mod:`app.services.backpressure`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.task_run import TaskRun
from app.models.task_state import transition_download_job
from app.services.backpressure import DownloadAdmissionError, enqueue_download_rq
from app.services.job_manifest import append_manifest_event
from app.services.job_progress import apply_download_progress
from app.services.redis_client import get_redis
from app.services.search_projection_outbox import request_search_projection
from app.services.tasks import TaskService

logger = logging.getLogger(__name__)

DISPATCH_META_KEY = "download_dispatch"
DISPATCH_PENDING = "pending"
DISPATCH_PUBLISHED = "published"
DISPATCH_FAILED = "failed"
DISPATCH_INVALID = "invalid"


@dataclass(frozen=True)
class PreparedDownloadDispatch:
    task: TaskRun
    queue_name: str
    rq_job_id: str
    attempt: int
    job_timeout: int = 7200
    delay_seconds: int | float | None = None
    action: str = "enqueue"


def deterministic_download_rq_job_id(job_id: UUID | str, attempt: int) -> str:
    """Return the stable RQ id for one durable publication attempt."""

    # RQ 2.x only accepts letters, numbers, underscores and dashes.  Colons
    # are also interpreted as execution-id separators by Job.fetch().
    return f"download-{job_id}-attempt-{max(1, int(attempt))}"


async def prepare_download_dispatch(
    db: AsyncSession,
    job: DownloadJob,
    *,
    queue_name: str,
    parent_task_id: UUID | None = None,
    job_timeout: int,
    delay_seconds: int | float | None = None,
    action: str = "enqueue",
) -> PreparedDownloadDispatch:
    """Persist an enqueue intent in DownloadJob/TaskRun before publication."""

    # Manual requests are allowed to enter the bounded queue while the NAS is
    # paused.  Make that state visible on the job instead of leaving users with
    # the generic "waiting for worker" message.  Automatic scans are rejected
    # earlier, but this also covers a pressure transition between admission
    # and publication and delayed retries from an already-running download.
    try:
        from app.services.resource_pressure import get_resource_pressure_snapshot

        pressure = await get_resource_pressure_snapshot()
        if pressure.get("status") == "paused":
            apply_download_progress(
                job,
                "enqueued",
                "Queued; waiting for resources to recover",
                publish=False,
            )
    except Exception:
        logger.debug("Unable to annotate queued resource-pressure wait", exc_info=True)

    service = TaskService(db)
    task = await service.get_by_subject("download_job", job.id)
    if task is None:
        task = await service.ensure_download_task(job, parent_task_id=parent_task_id)
    attempt = max(0, int(task.attempts or 0)) + 1
    rq_job_id = deterministic_download_rq_job_id(job.id, attempt)
    task.attempts = attempt
    task.queue_name = queue_name
    task_meta = dict(task.meta or {})
    task_meta[DISPATCH_META_KEY] = {
        "version": 1,
        "state": DISPATCH_PENDING,
        "queue_name": queue_name,
        "rq_job_id": rq_job_id,
        "job_timeout": int(job_timeout),
        "delay_seconds": delay_seconds,
        "action": action,
        "attempt": attempt,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    await service.update_task(
        task,
        status="enqueued",
        progress=job.progress_data if isinstance(job.progress_data, dict) else None,
        result=None,
        error=None,
        meta=task_meta,
        rq_job_id=rq_job_id,
        parent_task_id=parent_task_id,
    )
    append_manifest_event(
        job,
        "enqueue_prepared",
        queue=queue_name,
        action=action,
        attempt=attempt,
        rq_job_id=rq_job_id,
        job_timeout=int(job_timeout),
        delay_seconds=delay_seconds,
    )
    await db.flush()
    return PreparedDownloadDispatch(
        task=task,
        queue_name=queue_name,
        rq_job_id=rq_job_id,
        attempt=attempt,
        job_timeout=int(job_timeout),
        delay_seconds=delay_seconds,
        action=action,
    )


def _set_dispatch_state(
    task: TaskRun,
    state: str,
    *,
    error: str | None = None,
) -> None:
    meta = dict(task.meta or {})
    dispatch = dict(meta.get(DISPATCH_META_KEY) or {})
    dispatch["state"] = state
    dispatch["updated_at"] = datetime.now(timezone.utc).isoformat()
    if state == DISPATCH_PUBLISHED:
        dispatch["published_at"] = dispatch["updated_at"]
        dispatch.pop("last_error", None)
    elif error:
        dispatch["last_error"] = error[:1000]
    meta[DISPATCH_META_KEY] = dispatch
    task.meta = meta


def _validated_dispatch_payload(task: TaskRun, job: DownloadJob) -> dict[str, Any]:
    dispatch = dict((task.meta or {}).get(DISPATCH_META_KEY) or {})
    if dispatch.get("state") != DISPATCH_PENDING:
        raise ValueError("dispatch is not pending")
    queue_name = str(dispatch.get("queue_name") or "")
    if queue_name != "downloads" and not queue_name.startswith("downloads:"):
        raise ValueError("invalid download queue")
    if queue_name != str(task.queue_name or ""):
        raise ValueError("dispatch queue does not match TaskRun")
    attempt = int(dispatch.get("attempt") or 0)
    if attempt < 1:
        raise ValueError("invalid download dispatch attempt")
    expected_rq_job_id = deterministic_download_rq_job_id(job.id, attempt)
    rq_job_id = str(dispatch.get("rq_job_id") or "")
    if not rq_job_id or rq_job_id != str(task.rq_job_id or "") or rq_job_id != expected_rq_job_id:
        raise ValueError("dispatch RQ job id does not match its durable attempt")
    job_timeout = int(dispatch.get("job_timeout") or 0)
    if job_timeout <= 0:
        raise ValueError("invalid download job timeout")
    raw_delay = dispatch.get("delay_seconds")
    delay_seconds = None if raw_delay is None else float(raw_delay)
    if delay_seconds is not None and delay_seconds < 0:
        raise ValueError("invalid download delay")
    return {
        "queue_name": queue_name,
        "rq_job_id": rq_job_id,
        "job_timeout": job_timeout,
        "delay_seconds": delay_seconds,
        "action": str(dispatch.get("action") or "recovery"),
        "attempt": attempt,
    }


async def _persist_dispatch_failure(
    db: AsyncSession,
    job: DownloadJob,
    prepared: PreparedDownloadDispatch,
    exc: BaseException,
) -> None:
    message = f"Download enqueue failed: {exc}"
    transition_download_job(job, "failed", message)
    apply_download_progress(job, "failed", message, publish=False)
    _set_dispatch_state(prepared.task, DISPATCH_FAILED, error=message)
    append_manifest_event(
        job,
        "enqueue_failed",
        queue=prepared.queue_name,
        attempt=prepared.attempt,
        rq_job_id=prepared.rq_job_id,
        error=str(exc),
    )
    await TaskService(db).update_task(
        prepared.task,
        status="failed",
        progress=job.progress_data,
        error=message,
        meta=prepared.task.meta,
        rq_job_id=prepared.rq_job_id,
    )
    await request_search_projection(
        db,
        subscription_ids=(
            [job.subscription_id]
            if getattr(job, "subscription_id", None)
            else ()
        ),
    )
    await db.commit()


async def publish_prepared_download(
    db: AsyncSession,
    job: DownloadJob,
    prepared: PreparedDownloadDispatch,
    *,
    job_timeout: int | None = None,
    delay_seconds: int | float | None = None,
    action: str | None = None,
) -> Any:
    """Commit the durable intent, then atomically publish it to Redis.

    If publication is rejected, both the domain job and its TaskRun are moved
    to ``failed`` in a compensating transaction.  A crash/timeout after Redis
    accepts the request is safe because ``rq_job_id`` was committed first and
    the atomic publisher resolves that deterministic id before retrying.
    """

    effective_job_timeout = prepared.job_timeout if job_timeout is None else int(job_timeout)
    effective_delay = prepared.delay_seconds if delay_seconds is None else delay_seconds
    effective_action = prepared.action if action is None else action
    if effective_job_timeout != prepared.job_timeout:
        raise ValueError("publish timeout differs from the durable dispatch intent")
    if effective_delay != prepared.delay_seconds:
        raise ValueError("publish delay differs from the durable dispatch intent")
    if effective_action != prepared.action:
        raise ValueError("publish action differs from the durable dispatch intent")

    # This is the critical ordering boundary: no worker can see the RQ job
    # until DownloadJob + TaskRun + deterministic rq_job_id are durable.
    await db.commit()
    try:
        rq_job = await asyncio.to_thread(
            enqueue_download_rq,
            prepared.queue_name,
            "app.jobs.download.run_download_job",
            str(job.id),
            rq_job_id=prepared.rq_job_id,
            job_timeout=effective_job_timeout,
            delay_seconds=effective_delay,
        )
    except DownloadAdmissionError as exc:
        exc.details.setdefault("job_id", str(job.id))
        try:
            await _persist_dispatch_failure(db, job, prepared, exc)
        except Exception:
            logger.exception(
                "Failed to persist enqueue compensation for download %s",
                job.id,
            )
        raise
    except Exception as exc:
        admission = DownloadAdmissionError(
            "redis_unwritable",
            "Download queue rejected the job",
            details={"error_type": type(exc).__name__, "job_id": str(job.id)},
        )
        try:
            await _persist_dispatch_failure(db, job, prepared, admission)
        except Exception:
            logger.exception(
                "Failed to persist enqueue compensation for download %s",
                job.id,
            )
        raise admission from exc

    # Do not write the detached/pre-publication DownloadJob snapshot again
    # here.  The worker can start immediately after RQ's EXEC; a second commit
    # could otherwise overwrite manifest events written by that worker.  The
    # first transaction already contains enqueue_prepared + deterministic id,
    # and the worker's state transition is the durable acceptance evidence.
    logger.info(
        "Published download %s to %s as %s (action=%s delay=%s)",
        job.id,
        prepared.queue_name,
        prepared.rq_job_id,
        effective_action,
        effective_delay,
    )
    return rq_job


def _fetch_download_rq_job(rq_job_id: str, *, redis_client=None):
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        return Job.fetch(
            rq_job_id,
            connection=redis_client if redis_client is not None else get_redis(),
        )
    except NoSuchJobError:
        return None


def _rq_job_state(job) -> tuple[str, bool]:
    """Return the normalized RQ status and whether work is still executable."""

    get_status = getattr(job, "get_status", None)
    if get_status is None:
        # Compatibility with lightweight test fakes and legacy RQ objects. A
        # fetched record without a status API must not be failed speculatively.
        return "unknown", True
    status = get_status(refresh=True)
    status_value = str(getattr(status, "value", status)).lower()
    return status_value, status_value in {
        "queued",
        "started",
        "deferred",
        "scheduled",
    }


def _rq_terminal_diagnostics(job, rq_status: str) -> dict[str, Any]:
    """Build a small JSON-safe record for an unexpectedly terminal RQ job."""

    details: dict[str, Any] = {
        "code": "rq_terminal_record",
        "rq_status": rq_status,
    }
    for source_name, output_name, maximum in (
        ("description", "rq_description", 1000),
        ("exc_info", "rq_exc_info", 4000),
    ):
        value = getattr(job, source_name, None)
        if value:
            details[output_name] = str(value)[-maximum:]
    ended_at = getattr(job, "ended_at", None)
    if ended_at is not None:
        details["rq_ended_at"] = (
            ended_at.isoformat() if hasattr(ended_at, "isoformat") else str(ended_at)
        )
    return details


async def _locked_download_dispatch_rows(
    db: AsyncSession,
    *,
    job_id: UUID,
    task_id: UUID,
) -> tuple[DownloadJob | None, TaskRun | None]:
    job = (
        await db.execute(
            select(DownloadJob)
            .where(DownloadJob.id == job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        return None, None
    task = (
        await db.execute(
            select(TaskRun)
            .where(TaskRun.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    return job, task


async def _persist_terminal_rq_record(
    db: AsyncSession,
    job: DownloadJob,
    task: TaskRun,
    *,
    rq_job,
    rq_job_id: str,
    rq_status: str,
) -> bool:
    """Make an RQ-terminal/DB-enqueued split-brain state explicit."""

    job_id = UUID(str(job.id))
    task_id = UUID(str(task.id))
    diagnostics = _rq_terminal_diagnostics(rq_job, rq_status)
    diagnostics["rq_job_id"] = rq_job_id

    # The Redis lookup is deliberately outside row locks. Reacquire current
    # authoritative rows before making a terminal transition so a concurrent
    # retry/resume with a newer deterministic id always wins.
    await db.rollback()
    job, task = await _locked_download_dispatch_rows(
        db,
        job_id=job_id,
        task_id=task_id,
    )
    if (
        job is None
        or task is None
        or job.status != "enqueued"
        or task.status != "enqueued"
        or str(task.rq_job_id or "") != rq_job_id
        or ((task.meta or {}).get(DISPATCH_META_KEY) or {}).get("state")
        != DISPATCH_PENDING
    ):
        await db.rollback()
        return False

    message = (
        f"Download queue job {rq_job_id} is already terminal "
        f"(status={rq_status}) while the download remained enqueued"
    )
    transition_download_job(job, "failed", message)
    apply_download_progress(job, "failed", message, publish=False)
    _set_dispatch_state(task, DISPATCH_FAILED, error=message)
    task_meta = dict(task.meta or {})
    dispatch = dict(task_meta.get(DISPATCH_META_KEY) or {})
    dispatch["terminal_rq_status"] = rq_status
    dispatch["terminal_rq_detected_at"] = datetime.now(timezone.utc).isoformat()
    task_meta[DISPATCH_META_KEY] = dispatch
    task.meta = task_meta

    task_result = dict(getattr(task, "result_data", None) or {})
    task_result["download_dispatch_failure"] = diagnostics
    append_manifest_event(
        job,
        "rq_terminal_recovered",
        rq_job_id=rq_job_id,
        rq_status=rq_status,
    )
    await TaskService(db).update_task(
        task,
        status="failed",
        progress=job.progress_data,
        result=task_result,
        error=message,
        meta=task.meta,
        rq_job_id=rq_job_id,
    )
    await request_search_projection(
        db,
        subscription_ids=(
            [job.subscription_id] if getattr(job, "subscription_id", None) else ()
        ),
    )
    await db.commit()
    logger.error(
        "Terminal RQ record left download enqueued; marked failed "
        "task=%s job=%s rq_job=%s rq_status=%s",
        task.id,
        job.id,
        rq_job_id,
        rq_status,
    )
    return True


async def recover_download_dispatch_candidate(
    db: AsyncSession,
    task: TaskRun,
    job: DownloadJob,
    *,
    redis_client=None,
) -> str:
    """Recover one committed download publication intent.

    Capacity and Redis failures are deliberately non-terminal here: the
    original request is gone, so the durable outbox row remains pending for a
    later bounded recovery cycle instead of being compensated to ``failed``.
    """

    if task.status != "enqueued" or job.status != "enqueued":
        return "skipped"
    if ((task.meta or {}).get(DISPATCH_META_KEY) or {}).get("state") != DISPATCH_PENDING:
        return "skipped"
    task_id = str(task.id)
    job_id = str(job.id)
    try:
        payload = _validated_dispatch_payload(task, job)
    except (TypeError, ValueError) as exc:
        _set_dispatch_state(task, DISPATCH_INVALID, error=str(exc))
        await db.commit()
        logger.error("Invalid download dispatch outbox task=%s: %s", task_id, exc)
        return "invalid"

    try:
        existing = await asyncio.to_thread(
            _fetch_download_rq_job,
            payload["rq_job_id"],
            redis_client=redis_client,
        )
        if existing is not None:
            rq_status, is_active = await asyncio.to_thread(
                _rq_job_state,
                existing,
            )
            if not is_active:
                persisted = await _persist_terminal_rq_record(
                    db,
                    job,
                    task,
                    rq_job=existing,
                    rq_job_id=payload["rq_job_id"],
                    rq_status=rq_status,
                )
                return "terminal" if persisted else "skipped"
        if existing is None:
            await asyncio.to_thread(
                enqueue_download_rq,
                payload["queue_name"],
                "app.jobs.download.run_download_job",
                job_id,
                rq_job_id=payload["rq_job_id"],
                job_timeout=payload["job_timeout"],
                delay_seconds=payload["delay_seconds"],
                redis_client=redis_client,
            )
            outcome = "replayed"
        else:
            outcome = "existing"
    except Exception as exc:
        # End the read transaction and leave both domain/task states plus the
        # outbox metadata unchanged for the next recovery cycle.
        await db.rollback()
        logger.warning(
            "Download dispatch recovery deferred task=%s job=%s error=%s",
            task_id,
            job_id,
            type(exc).__name__,
        )
        return "deferred"

    # Refresh after Redis work so a concurrent worker/API transition is not
    # overwritten; only the TaskRun outbox metadata is changed.
    await db.refresh(task)
    if ((task.meta or {}).get(DISPATCH_META_KEY) or {}).get("state") == DISPATCH_PENDING:
        _set_dispatch_state(task, DISPATCH_PUBLISHED)
        await db.commit()
    else:
        await db.rollback()
    return outcome


async def recover_download_dispatch_outbox(
    db: AsyncSession,
    *,
    redis_client=None,
    limit: int | None = None,
    grace_seconds: float | None = None,
) -> dict[str, int]:
    """Replay a small oldest-first batch of lost download publications."""

    # Keep recovery deliberately small even if an environment override is
    # accidentally set too high.  Multiple cycles are safer on an 8 GB NAS
    # than one unbounded ORM/Redis burst.
    batch_limit = min(
        100,
        max(1, int(limit or settings.download_dispatch_recovery_batch_size)),
    )
    grace = max(
        1.0,
        float(
            grace_seconds
            if grace_seconds is not None
            else settings.download_dispatch_recovery_grace_seconds
        ),
    )
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace)
    stmt = (
        select(TaskRun, DownloadJob)
        .join(DownloadJob, DownloadJob.id == TaskRun.subject_id)
        .where(
            TaskRun.kind == "download",
            TaskRun.subject_type == "download_job",
            TaskRun.status == "enqueued",
            TaskRun.rq_job_id.isnot(None),
            TaskRun.updated_at <= cutoff,
            TaskRun.meta.contains({DISPATCH_META_KEY: {"state": DISPATCH_PENDING}}),
            DownloadJob.status == "enqueued",
        )
        .order_by(TaskRun.updated_at.asc(), TaskRun.id.asc())
        .limit(batch_limit)
    )
    rows = list((await db.execute(stmt)).all())
    result = {
        "checked": len(rows),
        "existing": 0,
        "replayed": 0,
        "terminal": 0,
        "deferred": 0,
        "invalid": 0,
        "skipped": 0,
    }
    for task, job in rows:
        await db.refresh(task)
        await db.refresh(job)
        outcome = await recover_download_dispatch_candidate(
            db,
            task,
            job,
            redis_client=redis_client,
        )
        result[outcome] += 1
    return result
