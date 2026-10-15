"""Download state application and its explicit TaskRun/receipt projection.

Callers select/lock the aggregate before applying a state and own the commit.
Workers use lock_download with the execution captured at claim time; callers
already holding domain locks use apply_download_state without another reread.
"""
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from app.models import DownloadJob, TaskRun
from app.models.task_state import transition_download_job
from app.services.job_manifest import append_manifest_event
from app.services.job_progress import apply_download_progress, publish_progress
from app.services.search_projection_outbox import request_search_projection
from app.services.sync_outcome import clear_download_job_outcome, download_job_outcome
from app.services.tasks import TaskService

_KEEP = object()


@dataclass(frozen=True)
class DownloadExecution:
    rq_job_id: str | None
    attempt: int


async def lock_download(db, job_id: UUID, *, execution: DownloadExecution | None = None, statuses=None, refresh=True):
    """Fence identity and SQL state while preserving artifact-before-parent order.

    A reconciler with staged scan metadata uses refresh=False and an explicit
    state filter; eligibility is still checked by SQL, before any autoflush.
    """
    with db.no_autoflush:
        statement = select(DownloadJob).where(DownloadJob.id == job_id)
        if statuses is not None:
            statement = statement.where(DownloadJob.status.in_(statuses))
        job = (await db.execute(statement.with_for_update().execution_options(populate_existing=refresh))).scalar_one_or_none()
        if job is None:
            return None
        if execution is not None:
            task = (await db.execute(select(TaskRun).where(TaskRun.subject_type == "download_job", TaskRun.subject_id == job_id)
                                    .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
            if task is None or (task.rq_job_id, int(task.attempts or 0)) != (execution.rq_job_id, execution.attempt):
                return None
    return job


async def project_download_state(db, job, *, task=None, result=_KEEP, reason_code=_KEEP, receipt=True):
    """Project current locked facts; repository writes never invoke this."""
    service = TaskService(db)
    task = task or await service.get_by_subject("download_job", job.id)
    if task is None:
        task = await service.ensure_download_task(job)
    values = {"status": job.status, "progress": job.progress_data, "error": job.error_log,
              "meta": {**(task.meta or {}), "retry_count": job.retry_count}}
    if result is not _KEEP:
        values["result"] = result
    elif job.status == "complete":
        values["result"] = download_job_outcome(job)
    if reason_code is not _KEEP:
        values["reason_code"] = reason_code
    await service.update_task(task, **values)
    terminal = job.status in {"complete", "failed", "stale", "cancelled"}
    if receipt and terminal:
        from app.services.operation_attention import upsert_repository_sync_receipt
        await upsert_repository_sync_receipt(db, job, status=job.status)
    # Shared subscription and membership documents exclude private job state.
    # Terminal transitions still refresh their materialized repository receipts.
    public_job = all(value is None for value in (
        job.owner_user_id,
        job.triggering_user_subscription_id,
        job.triggering_remote_account_id,
    ))
    if public_job or terminal:
        await request_search_projection(db, subscription_ids=[job.subscription_id] if job.subscription_id else ())
    await db.flush()
    return task


async def apply_download_state(db, job, status, *, error=_KEEP, progress=None, retry_count=None,
                               retry_error=None, reset_outcome=False, operator=None, note=None,
                               task=None, result=_KEEP, reason_code=_KEEP, receipt=True):
    """Apply one command to an already locked row, without committing."""
    old_status = job.status
    if retry_error is not None and status == "enqueued" and job.status != "failed":
        transition_download_job(job, "failed", retry_error)
    transition_download_job(job, status)
    if error is not _KEEP:
        job.error_log = error
    if retry_count is not None:
        job.retry_count = retry_count
    if status == "enqueued":
        job.last_heartbeat_at = None
    if reset_outcome:
        clear_download_job_outcome(job)
    if note:
        job.user_note = note
    if operator:
        job.operator_name, job.operator_action = operator, status
    if progress is not None:
        apply_download_progress(job, **progress, publish=False)
    append_manifest_event(job, "status_changed", from_status=old_status, to_status=status, error_log=job.error_log)
    await project_download_state(db, job, task=task, result=result, reason_code=reason_code, receipt=receipt)
    return job


async def claim_download(db, job_id: UUID, rq_job_id: str | None = None):
    job = await lock_download(db, job_id, statuses={"enqueued", "pending"})
    if job is None:
        return None
    task = await TaskService(db).get_by_subject("download_job", job.id)
    if task is not None and rq_job_id and task.rq_job_id and task.rq_job_id != rq_job_id:
        return None
    await apply_download_state(db, job, "downloading", progress={"stage": "configuring", "message": "Download worker started; preparing gallery-dl config"}, task=task)
    task = await TaskService(db).get_by_subject("download_job", job.id)
    if task.rq_job_id is None and rq_job_id:
        task.rq_job_id = rq_job_id
    return job, DownloadExecution(task.rq_job_id, int(task.attempts or 0))


async def commit_download_state(db, job):
    """Explicit top-level commit followed by best-effort progress delivery."""
    await db.commit()
    try:
        publish_progress(str(job.id), "download", job.progress_data or {})
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Committed download progress could not be published: %s", job.id, exc_info=True)


async def fail_or_retry_download(db, job, *, error: str, max_retries: int, kind: str,
                                 retry_message: str, failure_message: str):
    """One counter/state/evidence policy for automatic download retries."""
    from app.services.download_failure_evidence import record_unresolved_provider_failure
    count = int(job.retry_count or 0) + 1
    retry = count < max_retries
    if not retry:
        record_unresolved_provider_failure(job, kind=kind, reason=error, max_retries=max_retries)
    await apply_download_state(db, job, "enqueued" if retry else "failed", error=error,
                               retry_count=count, retry_error=error if retry else None,
                               progress={"stage": "enqueued" if retry else "failed", "message": retry_message if retry else failure_message})
    return count if retry else None
