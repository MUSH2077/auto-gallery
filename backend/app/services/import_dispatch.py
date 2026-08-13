"""Durable, idempotent publication of import jobs to RQ.

``ImportJob`` and its ``TaskRun`` are committed before Redis can expose work to
an import worker.  The TaskRun metadata is a small publication outbox: a stable
RQ id makes a lost enqueue response safe to replay, while temporary Redis
capacity or connectivity failures leave the import durably ``enqueued`` for
the periodic recovery loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import redis as redis_lib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_run import TaskRun
from app.models.task_state import transition_download_job, transition_import_job
from app.services.job_progress import apply_download_progress, apply_import_progress
from app.services.queue_admission import (
    QueueAdmissionError,
    checked_enqueue,
    checked_enqueue_in,
)
from app.services.redis_client import get_redis
from app.services.search_projection_outbox import request_search_projection
from app.services.tasks import TaskService

logger = logging.getLogger(__name__)

IMPORT_DISPATCH_META_KEY = "import_dispatch"
IMPORT_DISPATCH_PENDING = "pending"
IMPORT_DISPATCH_PUBLISHED = "published"
IMPORT_DISPATCH_INVALID = "invalid"
IMPORT_JOB_TIMEOUT_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class PreparedImportDispatch:
    task: TaskRun
    rq_job_id: str
    attempt: int
    job_timeout: int = IMPORT_JOB_TIMEOUT_SECONDS
    delay_seconds: float | None = None
    action: str = "enqueue"


def deterministic_import_rq_job_id(job_id: UUID | str, attempt: int) -> str:
    """Return the stable RQ id for one durable import publication attempt."""

    return f"import-{job_id}-attempt-{max(1, int(attempt))}"


async def prepare_import_dispatch(
    db: AsyncSession,
    job: ImportJob,
    *,
    parent_task_id: UUID | None = None,
    job_timeout: int = IMPORT_JOB_TIMEOUT_SECONDS,
    delay_seconds: int | float | None = None,
    action: str = "enqueue",
) -> PreparedImportDispatch:
    """Persist a new import publication attempt without touching Redis."""

    if int(job_timeout) <= 0:
        raise ValueError("import job timeout must be positive")
    # Adaptive waits count against RQ wall time.  Preserve compatibility with
    # callers that still pass the historical two-hour value, but never allow a
    # healthy constrained import to be SIGKILLed before its durable slices can
    # checkpoint and yield.
    job_timeout = max(int(job_timeout), IMPORT_JOB_TIMEOUT_SECONDS)
    normalized_delay = None if delay_seconds is None else float(delay_seconds)
    if normalized_delay is not None and normalized_delay < 0:
        raise ValueError("import dispatch delay must not be negative")
    prepared_at = datetime.now(timezone.utc)

    service = TaskService(db)
    task = await service.get_by_subject("import_job", job.id)
    if task is None:
        task = await service.ensure_import_task(job, parent_task_id=parent_task_id)

    attempt = max(0, int(task.attempts or 0)) + 1
    rq_job_id = deterministic_import_rq_job_id(job.id, attempt)
    task.attempts = attempt
    task.queue_name = "imports"
    meta = dict(task.meta or {})
    meta[IMPORT_DISPATCH_META_KEY] = {
        "version": 1,
        "state": IMPORT_DISPATCH_PENDING,
        "queue_name": "imports",
        "rq_job_id": rq_job_id,
        "job_timeout": int(job_timeout),
        "delay_seconds": normalized_delay,
        "available_at": (
            (prepared_at + timedelta(seconds=normalized_delay)).isoformat()
            if normalized_delay
            else None
        ),
        "action": str(action),
        "attempt": attempt,
        "prepared_at": prepared_at.isoformat(),
    }
    await service.update_task(
        task,
        status="enqueued",
        progress=job.progress_data if isinstance(job.progress_data, dict) else None,
        error=job.error_log,
        meta=meta,
        rq_job_id=rq_job_id,
        parent_task_id=parent_task_id,
    )
    await db.flush()
    return PreparedImportDispatch(
        task=task,
        rq_job_id=rq_job_id,
        attempt=attempt,
        job_timeout=int(job_timeout),
        delay_seconds=normalized_delay,
        action=str(action),
    )


def _dispatch_payload(task: TaskRun, job: ImportJob) -> dict[str, Any]:
    dispatch = dict((task.meta or {}).get(IMPORT_DISPATCH_META_KEY) or {})
    if dispatch.get("state") != IMPORT_DISPATCH_PENDING:
        raise ValueError("import dispatch is not pending")
    if dispatch.get("queue_name") != "imports" or task.queue_name != "imports":
        raise ValueError("invalid import dispatch queue")
    attempt = int(dispatch.get("attempt") or 0)
    if attempt < 1:
        raise ValueError("invalid import dispatch attempt")
    rq_job_id = str(dispatch.get("rq_job_id") or "")
    expected = deterministic_import_rq_job_id(job.id, attempt)
    if not rq_job_id or rq_job_id != str(task.rq_job_id or "") or rq_job_id != expected:
        raise ValueError("import dispatch RQ id does not match its durable attempt")
    job_timeout = int(dispatch.get("job_timeout") or 0)
    if job_timeout <= 0:
        raise ValueError("invalid import dispatch timeout")
    # Rolling upgrades can recover a durable pending intent written with the
    # historical two-hour timeout.  Clamp at publication as well as prepare so
    # those already-persisted attempts receive the adaptive-safe wall budget.
    job_timeout = max(job_timeout, IMPORT_JOB_TIMEOUT_SECONDS)
    raw_delay = dispatch.get("delay_seconds")
    delay_seconds = None if raw_delay is None else float(raw_delay)
    if delay_seconds is not None and delay_seconds < 0:
        raise ValueError("invalid import dispatch delay")
    available_at = dispatch.get("available_at")
    if available_at:
        available = datetime.fromisoformat(str(available_at))
        if available.tzinfo is None:
            raise ValueError("import dispatch available_at must be timezone-aware")
        delay_seconds = max(
            0.0,
            (available.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
        )
    return {
        "rq_job_id": rq_job_id,
        "attempt": attempt,
        "job_timeout": job_timeout,
        "delay_seconds": delay_seconds,
        "action": str(dispatch.get("action") or "recovery"),
    }


def _set_dispatch_state(
    task: TaskRun,
    state: str,
    *,
    error: BaseException | str | None = None,
) -> None:
    meta = dict(task.meta or {})
    dispatch = dict(meta.get(IMPORT_DISPATCH_META_KEY) or {})
    dispatch["state"] = state
    dispatch["updated_at"] = datetime.now(timezone.utc).isoformat()
    if state == IMPORT_DISPATCH_PUBLISHED:
        dispatch["published_at"] = dispatch["updated_at"]
        dispatch.pop("last_error", None)
    elif error is not None:
        dispatch["last_error"] = str(error)[:1000]
        dispatch["last_error_type"] = type(error).__name__
    meta[IMPORT_DISPATCH_META_KEY] = dispatch
    task.meta = meta


async def _persist_invalid_dispatch(
    db: AsyncSession,
    job: ImportJob,
    task: TaskRun,
    error: BaseException | str,
) -> None:
    """Make non-retryable publication errors terminal and explicit."""

    message = f"Import queue publication failed: {error}"
    _set_dispatch_state(task, IMPORT_DISPATCH_INVALID, error=error)
    if job.status == "enqueued":
        transition_import_job(job, "failed", message)
        apply_import_progress(job, "failed", message, publish=False)
    await TaskService(db).update_task(
        task,
        status="failed",
        progress=job.progress_data,
        error=message,
        meta=task.meta,
        rq_job_id=task.rq_job_id,
    )

    # Initial imports leave their parent download in importing.  Preserve the
    # old explicit-failure behaviour for code/serialization faults, while never
    # applying it to Redis capacity or connectivity failures.
    parent = await db.get(DownloadJob, job.download_job_id)
    if parent is not None and parent.status == "importing":
        transition_download_job(parent, "failed", message)
        apply_download_progress(parent, "failed", message, publish=False)
        await TaskService(db).update_subject(
            "download_job",
            parent.id,
            status="failed",
            progress=parent.progress_data,
            error=message,
        )
        await request_search_projection(
            db,
            subscription_ids=(
                [parent.subscription_id]
                if parent.subscription_id
                else ()
            ),
        )
    await db.commit()


def _fetch_import_rq_job(rq_job_id: str, *, redis_client=None):
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        return Job.fetch(
            rq_job_id,
            connection=redis_client if redis_client is not None else get_redis(),
        )
    except NoSuchJobError:
        return None


def _rq_job_is_active(job) -> bool:
    """Return whether an existing RQ record still owns executable work."""

    get_status = getattr(job, "get_status", None)
    if get_status is None:
        # Compatibility with simple test fakes and older RQ objects.
        return True
    status = get_status(refresh=True)
    status_value = str(getattr(status, "value", status)).lower()
    return status_value in {"queued", "started", "deferred", "scheduled"}


def _enqueue_import_rq(
    import_job_id: UUID | str,
    rq_job_id: str,
    job_timeout: int,
    *,
    delay_seconds: float | None = None,
    redis_client=None,
):
    from rq import Queue

    connection = redis_client if redis_client is not None else get_redis()
    queue = Queue(name="imports", connection=connection)
    enqueue_args = (
        "app.jobs.import_runner.run_import_job",
        str(import_job_id),
    )
    enqueue_kwargs = {
        "job_id": rq_job_id,
        "job_timeout": int(job_timeout),
    }
    if delay_seconds is not None and delay_seconds > 0:
        return checked_enqueue_in(
            queue,
            timedelta(seconds=delay_seconds),
            *enqueue_args,
            **enqueue_kwargs,
        )
    return checked_enqueue(queue, *enqueue_args, **enqueue_kwargs)


async def _locked_import_and_task(
    db: AsyncSession,
    import_job_id: UUID,
) -> tuple[ImportJob | None, TaskRun | None]:
    job = (
        await db.execute(
            select(ImportJob)
            .where(ImportJob.id == import_job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        return None, None
    task = (
        await db.execute(
            select(TaskRun)
            .where(
                TaskRun.subject_type == "import_job",
                TaskRun.subject_id == import_job_id,
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    return job, task


async def publish_prepared_import(
    db: AsyncSession,
    import_job_id: UUID | str,
    rq_job_id: str,
    *,
    redis_client=None,
) -> str:
    """Publish one committed import intent, or leave it pending for recovery.

    The domain and TaskRun rows are locked while Redis is checked and written.
    A worker may receive the RQ job immediately, but it cannot transition the
    ImportJob until the publication transaction records its outcome.  If this
    process dies after enqueue, recovery finds the deterministic RQ id instead
    of adding a duplicate queue entry.
    """

    job_uuid = UUID(str(import_job_id))
    # This is the ordering boundary: the outbox intent must be durable before
    # any RQ worker can observe it.
    await db.commit()
    job, task = await _locked_import_and_task(db, job_uuid)
    if job is None or task is None or job.status != "enqueued":
        await db.rollback()
        return "skipped"
    if task.rq_job_id != rq_job_id:
        # Another recovery attempt superseded this publisher after its intent
        # was committed.  It owns publication of the newer deterministic id.
        await db.rollback()
        return "skipped"
    dispatch_state = ((task.meta or {}).get(IMPORT_DISPATCH_META_KEY) or {}).get("state")
    if dispatch_state == IMPORT_DISPATCH_PUBLISHED:
        # A concurrent publisher serialized ahead of us on the same rows and
        # already committed proof that this deterministic id was accepted.
        await db.commit()
        return "existing"
    if dispatch_state == IMPORT_DISPATCH_INVALID:
        await db.rollback()
        return "invalid"
    try:
        payload = _dispatch_payload(task, job)
    except (TypeError, ValueError) as exc:
        await _persist_invalid_dispatch(db, job, task, exc)
        logger.error("Invalid import dispatch outbox job=%s: %s", job_uuid, exc)
        return "invalid"

    try:
        existing = await asyncio.to_thread(
            _fetch_import_rq_job,
            rq_job_id,
            redis_client=redis_client,
        )
        if existing is None:
            await asyncio.to_thread(
                _enqueue_import_rq,
                job_uuid,
                rq_job_id,
                payload["job_timeout"],
                delay_seconds=payload["delay_seconds"],
                redis_client=redis_client,
            )
            outcome = "replayed"
        else:
            outcome = "existing"
    except (QueueAdmissionError, redis_lib.RedisError, OSError) as exc:
        # Redis capacity, disconnection, and ambiguous publication failures are
        # retryable.  Keep the authoritative import/task enqueued and record a
        # diagnostic on the outbox instead of failing their parent download.
        _set_dispatch_state(task, IMPORT_DISPATCH_PENDING, error=exc)
        await db.commit()
        logger.warning(
            "Import dispatch deferred job=%s rq_job=%s error=%s",
            job_uuid,
            rq_job_id,
            type(exc).__name__,
        )
        return "deferred"
    except Exception as exc:
        # Argument/serialization/programming failures will not heal when Redis
        # pressure drops.  Surface them as an invalid durable intent instead of
        # misreporting them as a retryable infrastructure outage.
        await _persist_invalid_dispatch(db, job, task, exc)
        logger.exception(
            "Import dispatch invalid job=%s rq_job=%s",
            job_uuid,
            rq_job_id,
        )
        return "invalid"

    _set_dispatch_state(task, IMPORT_DISPATCH_PUBLISHED)
    await db.commit()
    logger.info(
        "Import dispatch %s job=%s rq_job=%s",
        outcome,
        job_uuid,
        rq_job_id,
    )
    return outcome


async def recover_import_dispatch_candidate(
    db: AsyncSession,
    import_job_id: UUID | str,
    *,
    redis_client=None,
    job_timeout: int = IMPORT_JOB_TIMEOUT_SECONDS,
) -> str:
    """Repair one old enqueued import whose RQ publication is absent."""

    job_uuid = UUID(str(import_job_id))
    await db.rollback()
    job, task = await _locked_import_and_task(db, job_uuid)
    if job is None or job.status != "enqueued":
        await db.rollback()
        return "skipped"

    # A response may have been lost after Redis accepted the job.  Resolve the
    # last durable id before creating another attempt.
    terminal_existing = False
    if task is not None and task.rq_job_id:
        try:
            existing = await asyncio.to_thread(
                _fetch_import_rq_job,
                task.rq_job_id,
                redis_client=redis_client,
            )
            if existing is not None:
                terminal_existing = not await asyncio.to_thread(
                    _rq_job_is_active,
                    existing,
                )
        except (QueueAdmissionError, redis_lib.RedisError, OSError) as exc:
            _set_dispatch_state(task, IMPORT_DISPATCH_PENDING, error=exc)
            await db.commit()
            logger.warning(
                "Import dispatch recovery deferred job=%s error=%s",
                job_uuid,
                type(exc).__name__,
            )
            return "deferred"
        except Exception as exc:
            await _persist_invalid_dispatch(db, job, task, exc)
            logger.exception("Invalid import RQ lookup job=%s", job_uuid)
            return "invalid"
        if existing is not None and not terminal_existing:
            _set_dispatch_state(task, IMPORT_DISPATCH_PUBLISHED)
            await db.commit()
            return "existing"

    if (
        task is not None
        and ((task.meta or {}).get(IMPORT_DISPATCH_META_KEY) or {}).get("state")
        == IMPORT_DISPATCH_INVALID
    ):
        await _persist_invalid_dispatch(
            db,
            job,
            task,
            "invalid durable import publication intent",
        )
        return "invalid"

    prepared: PreparedImportDispatch | None = None
    if task is not None:
        try:
            payload = _dispatch_payload(task, job)
        except (TypeError, ValueError):
            payload = None
        if payload is not None and not terminal_existing:
            prepared = PreparedImportDispatch(
                task=task,
                rq_job_id=payload["rq_job_id"],
                attempt=payload["attempt"],
                job_timeout=payload["job_timeout"],
                delay_seconds=payload["delay_seconds"],
                action=payload["action"],
            )
    if prepared is None:
        prepared = await prepare_import_dispatch(
            db,
            job,
            job_timeout=job_timeout,
        )

    # Commit the selected/new attempt, then reacquire the same rows inside the
    # common publisher.  Concurrent recovery loops serialize on ImportJob and
    # will either find this deterministic id or see a newer attempt.
    await db.commit()
    return await publish_prepared_import(
        db,
        job_uuid,
        prepared.rq_job_id,
        redis_client=redis_client,
    )
