"""Durable, bounded publication of download jobs to RQ.

The database row and its TaskRun projection are committed before Redis can
make the work visible to a worker.  Redis publication then uses the single
atomic admission path in :mod:`app.services.backpressure`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.task_run import TaskRun
from app.models.task_state import transition_download_job
from app.services.backpressure import (
    DownloadAdmissionError,
    enqueue_download_rq,
    is_transient_redis_admission_error,
)
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
TRANSIENT_ADMISSION_CODES = frozenset({
    "queue_saturated",
    "enqueue_busy",
    "redis_capacity",
    "redis_unwritable",
})
# The publication fence protects two small rows while one deterministic Redis
# publication is resolved. Bound database contention independently of Redis'
# own five-second admission-lock wait so API and recovery callers cannot hang.
DOWNLOAD_DISPATCH_LOCK_WAIT_MS = 1000


class _DownloadDispatchLockBusy(RuntimeError):
    """The exact-attempt publication fence could not be acquired in time."""


def _is_postgres_lock_timeout(exc: BaseException) -> bool:
    """Recognize PostgreSQL lock timeout by SQLSTATE, never error text."""

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (
            getattr(current, "sqlstate", None) == "55P03"
            or getattr(current, "pgcode", None) == "55P03"
        ):
            return True
        for nested in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def is_transient_download_dispatch_error(exc: Exception) -> bool:
    """Classify only recognized capacity and Redis transport failures."""

    if isinstance(exc, DownloadAdmissionError):
        if exc.transient is not None:
            return exc.transient
        return exc.code in TRANSIENT_ADMISSION_CODES
    return is_transient_redis_admission_error(exc)


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

    A definite publication rejection moves both the domain job and its TaskRun
    to ``failed`` in a compensating transaction. An uncertain acknowledgement
    remains pending because ``rq_job_id`` was committed first and bounded
    recovery can resolve that deterministic id safely.
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

    job_id = UUID(str(job.id))
    task_id = (
        UUID(str(prepared.task.id))
        if isinstance(job, DownloadJob) and isinstance(prepared.task, TaskRun)
        else None
    )
    rq_job_id = prepared.rq_job_id

    # This is the critical ordering boundary: no worker can see the RQ job
    # until DownloadJob + TaskRun + deterministic rq_job_id are durable.
    await db.commit()
    rows_locked = False
    if task_id is not None:
        try:
            locked_job, locked_task = await _locked_download_dispatch_rows(
                db,
                job_id=job_id,
                task_id=task_id,
            )
        except _DownloadDispatchLockBusy as exc:
            await db.rollback()
            raise DownloadAdmissionError(
                "enqueue_busy",
                "Download dispatch is already being published; retry shortly",
                details={
                    "job_id": str(job_id),
                    "rq_job_id": rq_job_id,
                    "lock_wait_ms": DOWNLOAD_DISPATCH_LOCK_WAIT_MS,
                },
                transient=True,
            ) from exc
        if locked_job is None or locked_task is None:
            await db.rollback()
            raise DownloadAdmissionError(
                "dispatch_superseded",
                "Download dispatch attempt no longer exists",
                status_code=409,
                details={"job_id": str(job_id), "rq_job_id": rq_job_id},
            )

        dispatch = dict((locked_task.meta or {}).get(DISPATCH_META_KEY) or {})
        same_attempt = (
            str(locked_task.rq_job_id or "") == prepared.rq_job_id
            and str(dispatch.get("rq_job_id") or "") == prepared.rq_job_id
        )
        if (
            same_attempt
            and dispatch.get("state") == DISPATCH_PENDING
            and locked_job.status == "enqueued"
            and locked_task.status == "enqueued"
        ):
            job = locked_job
            prepared = replace(prepared, task=locked_task)
            rows_locked = True
        elif same_attempt and (
            dispatch.get("state") == DISPATCH_PUBLISHED
            or (
                dispatch.get("state") == DISPATCH_PENDING
                and (
                    locked_job.status != "enqueued"
                    or locked_task.status != "enqueued"
                )
            )
        ):
            # A recovery publisher or worker already proved publication. The
            # caller does not consume the return value, and replaying here
            # could recreate a cleaned-up deterministic RQ record.
            await db.commit()
            return None
        else:
            await db.rollback()
            raise DownloadAdmissionError(
                "dispatch_superseded",
                "Download dispatch attempt is no longer pending",
                status_code=409,
                details={"job_id": str(job_id), "rq_job_id": rq_job_id},
            )

    try:
        rq_job = await asyncio.to_thread(
            enqueue_download_rq,
            prepared.queue_name,
            "app.jobs.download.run_download_job",
            str(job_id),
            rq_job_id=prepared.rq_job_id,
            job_timeout=effective_job_timeout,
            delay_seconds=effective_delay,
        )
    except DownloadAdmissionError as exc:
        exc.details.setdefault("job_id", str(job_id))
        if exc.publication_uncertain:
            try:
                accepted = await asyncio.to_thread(
                    _fetch_download_rq_job,
                    prepared.rq_job_id,
                )
            except Exception as confirmation_exc:
                # Preserve the pending outbox when neither success nor absence
                # can be established. A later bounded recovery retries the
                # same deterministic id.
                await db.rollback()
                logger.warning(
                    "Unable to confirm ambiguous direct download publication "
                    "job=%s rq_job=%s",
                    job_id,
                    prepared.rq_job_id,
                    exc_info=True,
                )
                raise exc from confirmation_exc
            if accepted is not None:
                if rows_locked:
                    _set_dispatch_state(prepared.task, DISPATCH_PUBLISHED)
                    await db.commit()
                return accepted
        try:
            await _persist_dispatch_failure(db, job, prepared, exc)
        except Exception:
            logger.exception(
                "Failed to persist enqueue compensation for download %s",
                job_id,
            )
        raise
    except Exception as exc:
        admission = DownloadAdmissionError(
            "redis_unwritable",
            "Download queue rejected the job",
            details={"error_type": type(exc).__name__, "job_id": str(job_id)},
        )
        try:
            await _persist_dispatch_failure(db, job, prepared, admission)
        except Exception:
            logger.exception(
                "Failed to persist enqueue compensation for download %s",
                job_id,
            )
        raise admission from exc

    if rows_locked:
        # Release the exact-attempt publication fence before the RQ worker
        # updates either row. This transaction contains locking reads only.
        await db.commit()

    # Do not write the detached/pre-publication DownloadJob snapshot again
    # here.  The worker can start immediately after RQ's EXEC; a second commit
    # could otherwise overwrite manifest events written by that worker.  The
    # first transaction already contains enqueue_prepared + deterministic id,
    # and the worker's state transition is the durable acceptance evidence.
    logger.info(
        "Published download %s to %s as %s (action=%s delay=%s)",
        job_id,
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
    # Global order: DownloadJob, TaskRun, then optional scheduler-batch parent.
    # SET LOCAL scopes the wait budget to this transaction and resets on its
    # commit/rollback, including pooled production connections.
    await db.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": str(max(1, int(DOWNLOAD_DISPATCH_LOCK_WAIT_MS)))},
    )
    try:
        job = (
            await db.execute(
                select(DownloadJob)
                .where(DownloadJob.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return None, None
        task = (
            await db.execute(
                select(TaskRun)
                .where(TaskRun.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return job, task
    except Exception as exc:
        if _is_postgres_lock_timeout(exc):
            raise _DownloadDispatchLockBusy from exc
        raise


async def _persist_terminal_rq_record(
    db: AsyncSession,
    job: DownloadJob,
    task: TaskRun,
    *,
    rq_job,
    rq_job_id: str,
    rq_status: str,
    locked_rows: tuple[DownloadJob, TaskRun] | None = None,
) -> bool:
    """Make an RQ-terminal/DB-enqueued split-brain state explicit."""

    job_id = UUID(str(job.id))
    task_id = UUID(str(task.id))
    diagnostics = _rq_terminal_diagnostics(rq_job, rq_status)
    diagnostics["rq_job_id"] = rq_job_id

    if locked_rows is None:
        # Legacy callers resolve Redis before this helper. Reacquire current
        # rows before terminalizing so a newer deterministic attempt wins.
        await db.rollback()
        job, task = await _locked_download_dispatch_rows(
            db,
            job_id=job_id,
            task_id=task_id,
        )
    else:
        job, task = locked_rows
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


async def _persist_dispatch_recovery_error(
    db: AsyncSession,
    job_id: UUID | str,
    task_id: UUID | str,
    *,
    rq_job_id: str,
    exc: Exception,
    locked_rows: tuple[DownloadJob, TaskRun] | None = None,
) -> bool:
    """Fail an exact pending attempt after a non-transient recovery fault."""

    job_id = UUID(str(job_id))
    task_id = UUID(str(task_id))
    if locked_rows is None:
        await db.rollback()
        job, task = await _locked_download_dispatch_rows(
            db,
            job_id=job_id,
            task_id=task_id,
        )
    else:
        job, task = locked_rows
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

    diagnostic_exc = (
        exc.__cause__
        if isinstance(exc, DownloadAdmissionError) and exc.__cause__ is not None
        else exc
    )
    message = (
        "Download dispatch recovery failed before publication could be "
        f"confirmed ({type(diagnostic_exc).__name__}: {diagnostic_exc})"
    )[:5000]
    transition_download_job(job, "failed", message)
    apply_download_progress(job, "failed", message, publish=False)
    _set_dispatch_state(task, DISPATCH_FAILED, error=message)
    task_result = dict(task.result_data or {})
    task_result["download_dispatch_failure"] = {
        "code": "dispatch_recovery_error",
        "error_type": type(diagnostic_exc).__name__,
        "rq_job_id": rq_job_id,
    }
    if isinstance(exc, DownloadAdmissionError):
        task_result["download_dispatch_failure"]["admission_code"] = exc.code
    await TaskService(db).update_task(
        task,
        status="failed",
        progress=job.progress_data,
        result=task_result,
        error=message,
        meta=task.meta,
        rq_job_id=rq_job_id,
        reason_code="dispatch_recovery_error",
    )
    append_manifest_event(
        job,
        "dispatch_recovery_failed",
        rq_job_id=rq_job_id,
        error_type=type(diagnostic_exc).__name__,
        admission_code=exc.code if isinstance(exc, DownloadAdmissionError) else None,
    )
    await request_search_projection(
        db,
        subscription_ids=[job.subscription_id] if job.subscription_id else (),
    )
    await db.commit()
    return True


async def recover_download_dispatch_candidate(db, task, job, *, redis_client=None) -> str:
    if (task.meta or {}).get("scheduler_batch_task_id"):
        from app.services.redis_budget import budget_redis
        with budget_redis(seconds=5, reserve_seconds=0):
            return await _recover_download_dispatch_candidate(db, task, job, redis_client=get_redis())
    return await _recover_download_dispatch_candidate(db, task, job, redis_client=redis_client)


async def _recover_download_dispatch_candidate(
    db: AsyncSession,
    task: TaskRun,
    job: DownloadJob,
    *,
    redis_client=None,
) -> str:
    """Recover one committed download publication intent.

    Recognized transient capacity, lock, and Redis transport failures remain
    pending for a later bounded recovery cycle. Definite nontransient failures
    become actionable only after the fixed RQ id is confirmed absent.
    """

    task_id = str(task.id)
    job_id = str(job.id)
    rows_locked = False
    if isinstance(task, TaskRun) and isinstance(job, DownloadJob):
        try:
            locked_job, locked_task = await _locked_download_dispatch_rows(
                db,
                job_id=UUID(job_id),
                task_id=UUID(task_id),
            )
        except _DownloadDispatchLockBusy:
            await db.rollback()
            logger.warning(
                "Download dispatch recovery deferred for publication fence "
                "task=%s job=%s",
                task_id,
                job_id,
            )
            return "deferred"
        if locked_job is None or locked_task is None:
            await db.rollback()
            return "skipped"
        job, task = locked_job, locked_task
        rows_locked = True

    if task.status != "enqueued" or job.status != "enqueued":
        if rows_locked:
            await db.rollback()
        return "skipped"
    if ((task.meta or {}).get(DISPATCH_META_KEY) or {}).get("state") != DISPATCH_PENDING:
        if rows_locked:
            await db.rollback()
        return "skipped"
    try:
        payload = _validated_dispatch_payload(task, job)
    except (TypeError, ValueError) as exc:
        _set_dispatch_state(task, DISPATCH_INVALID, error=str(exc))
        await db.commit()
        logger.error("Invalid download dispatch outbox task=%s: %s", task_id, exc)
        return "invalid"

    # A batch parent is the cancellation fence for *all* publications,
    # including outbox replay after a process restart. Take it after domain
    # locks; cancellation commits its parent-only fence before child cleanup.
    batch = None
    batch_parent_id = (task.meta or {}).get("scheduler_batch_task_id")
    if batch_parent_id:
        from app.models.scheduler_batch import SchedulerBatch
        batch = (await db.execute(select(SchedulerBatch).where(
            SchedulerBatch.task_id == UUID(batch_parent_id)))).scalar_one_or_none()
        if batch is not None:
            parent = (await db.execute(select(TaskRun).where(TaskRun.id == UUID(batch_parent_id))
                .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if parent is None or parent.status not in {"enqueued", "running", "recovering", "paused"}:
                await db.rollback()
                return "cancelled"

    try:
        existing = await asyncio.to_thread(
            _fetch_download_rq_job,
            payload["rq_job_id"],
            redis_client=redis_client,
        )
        if existing is not None:
            try:
                rq_status, is_active = await asyncio.to_thread(
                    _rq_job_state,
                    existing,
                )
            except Exception:
                # The deterministic RQ record itself is publication proof.
                # A faulty status adapter must not compensate work that Redis
                # can still deliver.
                logger.exception(
                    "Unable to inspect existing download RQ status task=%s job=%s",
                    task_id,
                    job_id,
                )
                rq_status, is_active = "unknown", True
            if not is_active:
                persisted = await _persist_terminal_rq_record(
                    db,
                    job,
                    task,
                    rq_job=existing,
                    rq_job_id=payload["rq_job_id"],
                    rq_status=rq_status,
                    locked_rows=(job, task) if rows_locked else None,
                )
                return "terminal" if persisted else "skipped"
        if existing is None:
            from app.jobs.admin_operations import _await_admin_file_finalizer
            await _await_admin_file_finalizer(asyncio.to_thread(
                enqueue_download_rq,
                payload["queue_name"],
                "app.jobs.download.run_download_job",
                job_id,
                rq_job_id=payload["rq_job_id"],
                job_timeout=payload["job_timeout"],
                delay_seconds=payload["delay_seconds"],
                redis_client=redis_client,
            ))
            outcome = "replayed"
        else:
            outcome = "existing"
    except Exception as exc:
        if is_transient_download_dispatch_error(exc):
            # Leave both domain/task states and outbox metadata unchanged for
            # the next recovery cycle, releasing the exact-attempt locks.
            await db.rollback()
            logger.warning(
                "Download dispatch recovery deferred task=%s job=%s error=%s",
                task_id,
                job_id,
                type(exc).__name__,
            )
            return "deferred"
        late_existing = None
        try:
            late_existing = await asyncio.to_thread(
                _fetch_download_rq_job,
                payload["rq_job_id"],
                redis_client=redis_client,
            )
        except Exception:
            logger.warning(
                "Unable to complete final fixed-id lookup after dispatch error "
                "task=%s job=%s",
                task_id,
                job_id,
                exc_info=True,
            )
            # A failed lookup never proves that the deterministic RQ job is
            # absent, regardless of the original error category. Keep the
            # exact durable attempt pending until its state can be checked.
            await db.rollback()
            return "deferred"
        if late_existing is not None:
            logger.warning(
                "Recovered late fixed-id publication proof task=%s job=%s rq_job=%s",
                task_id,
                job_id,
                payload["rq_job_id"],
            )
            existing = late_existing
            outcome = "existing"
        else:
            logger.exception(
                "Download dispatch recovery failed task=%s job=%s error=%s",
                task_id,
                job_id,
                type(exc).__name__,
            )
            persisted = await _persist_dispatch_recovery_error(
                db,
                job_id,
                task_id,
                rq_job_id=payload["rq_job_id"],
                exc=exc,
                locked_rows=(job, task) if rows_locked else None,
            )
            return "error" if persisted else "skipped"

    # Refresh after Redis work so a concurrent worker/API transition is not
    # overwritten; only the TaskRun outbox metadata is changed.
    if batch is None:
        await db.refresh(task)
    else:
        # A very fast worker may already have compacted the operational row.
        # Its durable receipt remains authoritative; never recreate the dispatch.
        task = (await db.execute(select(TaskRun).where(TaskRun.id == UUID(task_id))
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if task is None:
            from app.models.repository_sync_receipt import RepositorySyncReceipt
            receipt = (await db.execute(select(RepositorySyncReceipt.id).where(
                RepositorySyncReceipt.source_download_job_id == UUID(job_id)))).scalar_one_or_none()
            await db.commit()
            return "terminal" if receipt else "skipped"
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
        "cancelled": 0,
        "error": 0,
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
