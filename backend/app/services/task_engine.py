"""
Task Engine — centralized task lifecycle manager.

The TaskEngine is the single authority on valid state transitions for both
download and import jobs. It replaces the scattered logic that previously
lived in ``DownloadService``, ``download.py``, ``import_runner.py``, and API
route handlers.

Key responsibilities:
  - Validate and execute state transitions
  - Publish status changes via Redis pub/sub (for WebSocket push)
  - Send control signals (pause / cancel / resume) to running workers
  - Batch operations by filter criteria (source, status, subscription)
  - Heartbeat-based stale detection
"""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.subscription_source import SubscriptionSource
from app.models.task_run import TaskRun
from app.models.task_state import (
    DOWNLOAD_ENQUEUED,
    DOWNLOAD_PAUSED,
    DOWNLOAD_CANCELLED,
    DOWNLOAD_PAUSABLE_STATUSES,
    DOWNLOAD_CANCELLABLE_STATUSES,
    DOWNLOAD_RETRYABLE_STATUSES,
    IMPORT_ENQUEUED,
    IMPORT_PAUSED,
    IMPORT_CANCELLED,
    IMPORT_RUNNING,
    IMPORT_PAUSABLE_STATUSES,
    IMPORT_CANCELLABLE_STATUSES,
    IMPORT_FAILED,
    IMPORT_STALE,
    transition_download_job,
    transition_import_job,
)
from app.repositories.download_job import DownloadJobRepository
from app.services.job_manifest import append_manifest_event
from app.services.job_manifest import update_manifest
from app.services.job_progress import apply_download_progress
from app.services.download_dispatch import prepare_download_dispatch, publish_prepared_download
from app.services.import_dispatch import prepare_import_dispatch, publish_prepared_import
from app.services.redis_pubsub import TaskChannel, TaskEventPublisher
from app.services.redis_client import get_redis
from app.services.sync_outcome import clear_download_job_outcome
from app.services.search_projection_outbox import request_search_projection
from app.services.tasks import TaskService
from app.services.import_lifecycle import project_import_pipeline_state

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200
HEARTBEAT_TIMEOUT = TaskEventPublisher.HEARTBEAT_TTL
STALE_START_GRACE_SECONDS = TaskEventPublisher.HEARTBEAT_TTL
STALE_SCAN_LIMIT = 100


def _heartbeat_presence(redis_client, task_ids: list[UUID]) -> list[bool]:
    """Read all heartbeat TTL keys in one Redis round trip.

    Exceptions deliberately propagate.  The caller treats an unreadable Redis
    as unknown liveness and marks nothing; database age is never a substitute
    for the authoritative TTL key.
    """

    pipeline = redis_client.pipeline(transaction=False)
    for task_id in task_ids:
        pipeline.exists(f"task:{task_id}:heartbeat_ts")
    values = pipeline.execute()
    if len(values) != len(task_ids):
        raise RuntimeError("Redis heartbeat pipeline returned an incomplete result")
    return [bool(value) for value in values]


def _outside_start_grace(
    started_or_created_at: datetime | None,
    now: datetime,
    *,
    grace_seconds: int = STALE_START_GRACE_SECONDS,
) -> bool:
    """Return true only after a worker had enough time to publish heartbeat 1."""

    if started_or_created_at is None:
        return False
    anchor = started_or_created_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor <= now - timedelta(seconds=max(1, int(grace_seconds)))


class TaskEngineError(ValueError):
    """Raised when a task engine operation cannot be completed."""
    pass


class TaskEngine:
    """Centralized task lifecycle manager.

    All state-changing methods:
    1. Validate the transition against the state machine
    2. Update the database
    3. Append a manifest event
    4. Publish the change via Redis pub/sub
    5. Send control signals to workers when needed (pause / cancel)
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = DownloadJobRepository(db)

    # ── Lookup helpers ────────────────────────

    async def _get_download(self, job_id: UUID) -> DownloadJob:
        job = await self.repo.get(job_id)
        if not job:
            raise TaskEngineError(f"DownloadJob {job_id} not found")
        return job

    async def _get_import(self, job_id: UUID) -> ImportJob:
        job = await self.repo.get_import(job_id)
        if not job:
            raise TaskEngineError(f"ImportJob {job_id} not found")
        return job

    async def _latest_import_for_download(
        self,
        download_job_id: UUID,
        *,
        statuses: set[str] | frozenset[str] | None = None,
    ) -> ImportJob | None:
        # A few pure queue-routing unit tests intentionally use a minimal DB
        # double with only get/flush/commit. Production AsyncSession always
        # supplies execute; the double has no child rows to delegate to.
        if not hasattr(self.db, "execute"):
            return None
        statement = (
            select(ImportJob)
            .where(ImportJob.download_job_id == download_job_id)
            .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
            .limit(1)
        )
        if statuses:
            statement = statement.where(ImportJob.status.in_(statuses))
        return (await self.db.execute(statement)).scalar_one_or_none()

    async def _project_import_state(
        self,
        job: ImportJob,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        if not hasattr(self.db, "execute"):
            await self.db.flush()
            return
        await project_import_pipeline_state(
            self.db,
            job,
            status=status,
            error=error,
        )

    # ── Pause ─────────────────────────────────

    async def pause_download(
        self, job_id: UUID, *, note: str | None = None, operator: str | None = None,
    ) -> dict[str, Any]:
        """Pause a download job. Sends SIGTERM signal to the running process group."""
        job = await self._get_download(job_id)

        if job.status == "importing":
            child = await self._latest_import_for_download(
                job.id,
                statuses={IMPORT_ENQUEUED, IMPORT_RUNNING},
            )
            if child is not None:
                delegated = await self.pause_import(
                    child.id,
                    note=note,
                    operator=operator,
                )
                return {
                    "job_id": str(job.id),
                    "status": DOWNLOAD_PAUSED,
                    "delegated_to": delegated["job_id"],
                }

        if job.status not in DOWNLOAD_PAUSABLE_STATUSES:
            raise TaskEngineError(
                f"Cannot pause download job with status '{job.status}'. "
                f"Allowed: {sorted(DOWNLOAD_PAUSABLE_STATUSES)}"
            )

        old_status = job.status
        job = await self._apply_transition(job, DOWNLOAD_PAUSED, operator=operator, note=note)

        TaskEventPublisher.send_control(str(job_id), "pause", reason=note)
        TaskEventPublisher.publish_status_change(
            str(job_id), "download", old_status, DOWNLOAD_PAUSED,
            operator=operator, note=note,
        )

        logger.info("Download job %s paused by %s", job_id, operator)
        return {"job_id": str(job_id), "status": job.status}

    async def pause_import(
        self, job_id: UUID, *, note: str | None = None, operator: str | None = None,
    ) -> dict[str, Any]:
        """Pause an import job. The import loop checks for pause between works."""
        job = await self._get_import(job_id)

        if job.status not in IMPORT_PAUSABLE_STATUSES:
            raise TaskEngineError(
                f"Cannot pause import job with status '{job.status}'. "
                f"Allowed: {sorted(IMPORT_PAUSABLE_STATUSES)}"
            )

        old_status = job.status
        job.status = IMPORT_PAUSED
        if note:
            job.user_note = note
        if operator:
            job.operator_name = operator
            job.operator_action = "pause"
        await self._project_import_state(
            job,
            status=IMPORT_PAUSED,
            error=None,
        )

        TaskEventPublisher.send_control(str(job_id), "pause", reason=note)
        TaskEventPublisher.publish_status_change(
            str(job_id), "import", old_status, IMPORT_PAUSED,
            operator=operator, note=note,
        )

        return {"job_id": str(job_id), "status": job.status}

    # ── Resume ────────────────────────────────

    async def resume_download(
        self, job_id: UUID, *, operator: str | None = None,
    ) -> dict[str, Any]:
        """Resume a paused download job by re-enqueuing it."""
        job = await self._get_download(job_id)

        if job.status == DOWNLOAD_PAUSED:
            child = await self._latest_import_for_download(
                job.id,
                statuses={IMPORT_PAUSED},
            )
            if child is not None:
                delegated = await self.resume_import(child.id, operator=operator)
                return {
                    "job_id": str(job.id),
                    "status": "importing",
                    "delegated_to": delegated["job_id"],
                }

        if job.status != DOWNLOAD_PAUSED:
            raise TaskEngineError(
                f"Cannot resume download job with status '{job.status}'. Must be 'paused'."
            )

        old_status = job.status
        job = await self._apply_transition(job, DOWNLOAD_ENQUEUED, operator=operator)
        apply_download_progress(
            job,
            "enqueued",
            "Queued; waiting for download worker",
            publish=False,
        )
        prepared = await prepare_download_dispatch(
            self.db,
            job,
            queue_name="downloads",
            job_timeout=RQ_JOB_TIMEOUT,
            action="resume",
        )
        await publish_prepared_download(
            self.db,
            job,
            prepared,
            job_timeout=RQ_JOB_TIMEOUT,
            action="resume",
        )

        TaskEventPublisher.publish_status_change(
            str(job_id), "download", old_status, DOWNLOAD_ENQUEUED, operator=operator,
        )

        return {"job_id": str(job_id), "status": job.status}

    async def resume_import(
        self, job_id: UUID, *, operator: str | None = None,
    ) -> dict[str, Any]:
        """Resume a paused import job by re-enqueuing it."""
        job = await self._get_import(job_id)

        if job.status != IMPORT_PAUSED:
            raise TaskEngineError(
                f"Cannot resume import job with status '{job.status}'. Must be 'paused'."
            )

        old_status = job.status
        job.status = IMPORT_ENQUEUED
        if operator:
            job.operator_name = operator
            job.operator_action = "resume"
        await self._project_import_state(
            job,
            status=IMPORT_ENQUEUED,
            error=None,
        )

        prepared = await prepare_import_dispatch(
            self.db,
            job,
            job_timeout=RQ_JOB_TIMEOUT,
            action="resume",
        )
        publication = await publish_prepared_import(
            self.db,
            job.id,
            prepared.rq_job_id,
        )
        if publication == "invalid":
            raise TaskEngineError("Import resume queue publication is invalid")

        TaskEventPublisher.publish_status_change(
            str(job_id), "import", old_status, IMPORT_ENQUEUED, operator=operator,
        )

        return {"job_id": str(job_id), "status": job.status}

    # ── Cancel ────────────────────────────────

    async def cancel_download(
        self, job_id: UUID, *, note: str | None = None, operator: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a download job. Terminal — cannot be retried."""
        job = await self._get_download(job_id)

        if job.status in {"importing", DOWNLOAD_PAUSED}:
            child = await self._latest_import_for_download(
                job.id,
                statuses={IMPORT_ENQUEUED, IMPORT_RUNNING, IMPORT_PAUSED, IMPORT_FAILED, IMPORT_STALE},
            )
            if child is not None:
                delegated = await self.cancel_import(
                    child.id,
                    note=note,
                    operator=operator,
                )
                return {
                    "job_id": str(job.id),
                    "status": DOWNLOAD_CANCELLED,
                    "delegated_to": delegated["job_id"],
                }

        if job.status not in DOWNLOAD_CANCELLABLE_STATUSES:
            raise TaskEngineError(
                f"Cannot cancel download job with status '{job.status}'. "
                f"Allowed: {sorted(DOWNLOAD_CANCELLABLE_STATUSES)}"
            )

        old_status = job.status
        job = await self._apply_transition(job, DOWNLOAD_CANCELLED, operator=operator, note=note)

        TaskEventPublisher.send_control(str(job_id), "cancel", reason=note)
        TaskEventPublisher.publish_status_change(
            str(job_id), "download", old_status, DOWNLOAD_CANCELLED,
            operator=operator, note=note,
        )

        logger.info("Download job %s cancelled by %s", job_id, operator)
        return {"job_id": str(job_id), "status": job.status}

    async def cancel_import(
        self, job_id: UUID, *, note: str | None = None, operator: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an import job. Terminal — cannot be retried."""
        job = await self._get_import(job_id)

        if job.status not in IMPORT_CANCELLABLE_STATUSES:
            raise TaskEngineError(
                f"Cannot cancel import job with status '{job.status}'. "
                f"Allowed: {sorted(IMPORT_CANCELLABLE_STATUSES)}"
            )

        old_status = job.status
        job.status = IMPORT_CANCELLED
        if note:
            job.user_note = note
        if operator:
            job.operator_name = operator
            job.operator_action = "cancel"
        await self._project_import_state(
            job,
            status=IMPORT_CANCELLED,
            error=None,
        )

        TaskEventPublisher.send_control(str(job_id), "cancel", reason=note)
        TaskEventPublisher.publish_status_change(
            str(job_id), "import", old_status, IMPORT_CANCELLED,
            operator=operator, note=note,
        )

        return {"job_id": str(job_id), "status": job.status}

    # ── Retry ─────────────────────────────────

    async def retry_download(
        self, job_id: UUID, *, operator: str | None = None,
    ) -> dict[str, Any]:
        """Retry a download job. Resets retry_count and clears error_log."""
        job = await self._get_download(job_id)

        if job.status in {"failed", "stale"}:
            child = await self._latest_import_for_download(
                job.id,
                statuses={IMPORT_FAILED, IMPORT_STALE},
            )
            if child is not None:
                delegated = await self.retry_import(child.id, operator=operator)
                return {
                    "job_id": str(job.id),
                    "status": "importing",
                    "delegated_to": delegated["job_id"],
                }

        if job.status not in DOWNLOAD_RETRYABLE_STATUSES:
            raise TaskEngineError(
                f"Cannot retry download job with status '{job.status}'. "
                f"Allowed: {sorted(DOWNLOAD_RETRYABLE_STATUSES)}"
            )

        old_status = job.status
        job = await self._apply_transition(job, DOWNLOAD_ENQUEUED, operator=operator)
        job.retry_count = 0
        job.error_log = None
        clear_download_job_outcome(job)
        subscription_source_id = getattr(job, "subscription_source_id", None)
        source = await self.db.get(SubscriptionSource, subscription_source_id) if subscription_source_id else None
        update_manifest(job, had_sync_baseline=bool(source and source.last_synced_at))
        apply_download_progress(
            job,
            "enqueued",
            "Queued; waiting for download worker",
            publish=False,
        )
        prepared = await prepare_download_dispatch(
            self.db,
            job,
            queue_name="downloads",
            job_timeout=RQ_JOB_TIMEOUT,
            action="retry",
        )
        await publish_prepared_download(
            self.db,
            job,
            prepared,
            job_timeout=RQ_JOB_TIMEOUT,
            action="retry",
        )

        TaskEventPublisher.publish_status_change(
            str(job_id), "download", old_status, DOWNLOAD_ENQUEUED, operator=operator,
        )

        return {"job_id": str(job_id), "status": job.status}

    async def retry_import(
        self, job_id: UUID, *, operator: str | None = None,
    ) -> dict[str, Any]:
        """Retry a failed or stale import job."""
        job = await self._get_import(job_id)

        if job.status in {IMPORT_ENQUEUED, IMPORT_RUNNING, IMPORT_PAUSED, IMPORT_CANCELLED}:
            raise TaskEngineError(
                f"Cannot retry import job with status '{job.status}'."
            )

        old_status = job.status
        job.status = IMPORT_ENQUEUED
        job.import_retry_count = (job.import_retry_count or 0) + 1
        job.error_log = None
        if operator:
            job.operator_name = operator
            job.operator_action = "retry"
        await self._project_import_state(
            job,
            status=IMPORT_ENQUEUED,
            error=None,
        )

        prepared = await prepare_import_dispatch(
            self.db,
            job,
            job_timeout=RQ_JOB_TIMEOUT,
            action="retry",
        )
        publication = await publish_prepared_import(
            self.db,
            job.id,
            prepared.rq_job_id,
        )
        if publication == "invalid":
            raise TaskEngineError("Import retry queue publication is invalid")

        TaskEventPublisher.publish_status_change(
            str(job_id), "import", old_status, IMPORT_ENQUEUED, operator=operator,
        )

        return {"job_id": str(job_id), "status": job.status}

    # ── Delete ────────────────────────────────

    async def delete_download(self, job_id: UUID) -> None:
        """Delete a download job. Refuses if currently importing."""
        job = await self._get_download(job_id)

        if job.status == "importing":
            raise TaskEngineError(
                "Cannot delete download job while import is in progress. Cancel it first."
            )

        task_id = str(job.id)
        subscription_id = job.subscription_id
        await self.db.delete(job)
        await request_search_projection(
            self.db,
            subscription_ids=[subscription_id] if subscription_id else (),
        )
        await self.db.commit()

        TaskEventPublisher.publish_status_change(
            task_id, "download", job.status, "deleted",
        )
        logger.info("Download job %s deleted", task_id)

    async def delete_import(self, job_id: UUID) -> None:
        """Delete an import job. Refuses if enqueued or running."""
        job = await self._get_import(job_id)

        if job.status in {IMPORT_ENQUEUED, IMPORT_RUNNING}:
            raise TaskEngineError(
                "Cannot delete import job while it is enqueued or running. Cancel it first."
            )

        task_id = str(job.id)
        await self.db.delete(job)
        await self.db.commit()

        TaskEventPublisher.publish_status_change(
            task_id, "import", job.status, "deleted",
        )

    # ── Priority ──────────────────────────────

    async def set_priority_download(
        self, job_id: UUID, priority: int, *, operator: str | None = None,
    ) -> dict[str, Any]:
        job = await self._get_download(job_id)
        job.priority = priority
        if operator:
            job.operator_name = operator
            job.operator_action = "set_priority"
        await self.db.flush()
        return {"job_id": str(job_id), "priority": priority}

    async def set_priority_import(
        self, job_id: UUID, priority: int, *, operator: str | None = None,
    ) -> dict[str, Any]:
        job = await self._get_import(job_id)
        job.priority = priority
        if operator:
            job.operator_name = operator
            job.operator_action = "set_priority"
        await self.db.flush()
        return {"job_id": str(job_id), "priority": priority}

    # ── Stale detection ───────────────────────

    async def detect_stale_tasks(
        self,
        *,
        redis_client=None,
        now: datetime | None = None,
        limit: int = STALE_SCAN_LIMIT,
    ) -> int:
        """Mark active domain jobs stale when their Redis heartbeat TTL is gone.

        Redis is the sole liveness authority.  PostgreSQL supplies only the
        bounded set of currently executing jobs and their TaskRun start time;
        ``last_heartbeat_at`` is intentionally ignored because heartbeat
        threads cannot safely maintain it.  A Redis read failure is fail-safe:
        no domain row or TaskRun is changed.
        """

        checked_at = now or datetime.now(timezone.utc)
        bounded_limit = max(1, min(int(limit), STALE_SCAN_LIMIT))

        download_task = aliased(TaskRun, name="stale_download_task")
        download_candidates = list((await self.db.execute(
            select(
                DownloadJob.id,
                func.coalesce(
                    download_task.started_at,
                    download_task.created_at,
                    DownloadJob.created_at,
                ).label("started_or_created_at"),
            )
            .outerjoin(
                download_task,
                and_(
                    download_task.subject_type == "download_job",
                    download_task.subject_id == DownloadJob.id,
                ),
            )
            # ``importing`` is supervised by the child ImportJob heartbeat,
            # not by a second key for the parent DownloadJob.
            .where(DownloadJob.status == "downloading")
            .order_by(DownloadJob.created_at, DownloadJob.id)
            .limit(bounded_limit)
        )).all())

        import_task = aliased(TaskRun, name="stale_import_task")
        import_candidates = list((await self.db.execute(
            select(
                ImportJob.id,
                func.coalesce(
                    import_task.started_at,
                    import_task.created_at,
                    ImportJob.created_at,
                ).label("started_or_created_at"),
            )
            .outerjoin(
                import_task,
                and_(
                    import_task.subject_type == "import_job",
                    import_task.subject_id == ImportJob.id,
                ),
            )
            .where(ImportJob.status == IMPORT_RUNNING)
            .order_by(ImportJob.created_at, ImportJob.id)
            .limit(bounded_limit)
        )).all())

        candidates: list[tuple[str, UUID]] = [
            ("download", task_id)
            for task_id, anchor in download_candidates
            if _outside_start_grace(anchor, checked_at)
        ]
        candidates.extend(
            ("import", task_id)
            for task_id, anchor in import_candidates
            if _outside_start_grace(anchor, checked_at)
        )
        if not candidates:
            return 0

        try:
            client = redis_client if redis_client is not None else get_redis()
            heartbeat_exists = await asyncio.to_thread(
                _heartbeat_presence,
                client,
                [task_id for _task_type, task_id in candidates],
            )
        except Exception:
            logger.warning(
                "Stale detection skipped because Redis heartbeat state is unreadable",
                exc_info=True,
            )
            return 0

        missing_download_ids = [
            task_id
            for (task_type, task_id), exists in zip(
                candidates,
                heartbeat_exists,
                strict=True,
            )
            if task_type == "download" and not exists
        ]
        missing_import_ids = [
            task_id
            for (task_type, task_id), exists in zip(
                candidates,
                heartbeat_exists,
                strict=True,
            )
            if task_type == "import" and not exists
        ]
        if not missing_download_ids and not missing_import_ids:
            return 0

        # Re-read and lock only missing-heartbeat rows.  A worker may have
        # completed between the cheap candidate scan and Redis pipeline, so
        # the active-status predicates are repeated before any mutation.
        locked_download_task = aliased(TaskRun, name="locked_download_task")
        locked_download_rows = []
        if missing_download_ids:
            locked_download_rows = list((await self.db.execute(
                select(DownloadJob, locked_download_task)
                .outerjoin(
                    locked_download_task,
                    and_(
                        locked_download_task.subject_type == "download_job",
                        locked_download_task.subject_id == DownloadJob.id,
                    ),
                )
                .where(
                    DownloadJob.id.in_(missing_download_ids),
                    DownloadJob.status == "downloading",
                )
                .order_by(DownloadJob.id)
                .with_for_update(of=DownloadJob, skip_locked=True)
            )).all())

        locked_import_task = aliased(TaskRun, name="locked_import_task")
        locked_parent_task = aliased(TaskRun, name="locked_parent_task")
        locked_import_rows = []
        if missing_import_ids:
            locked_import_rows = list((await self.db.execute(
                select(
                    ImportJob,
                    locked_import_task,
                    DownloadJob,
                    locked_parent_task,
                )
                .join(DownloadJob, DownloadJob.id == ImportJob.download_job_id)
                .outerjoin(
                    locked_import_task,
                    and_(
                        locked_import_task.subject_type == "import_job",
                        locked_import_task.subject_id == ImportJob.id,
                    ),
                )
                .outerjoin(
                    locked_parent_task,
                    and_(
                        locked_parent_task.subject_type == "download_job",
                        locked_parent_task.subject_id == DownloadJob.id,
                    ),
                )
                .where(
                    ImportJob.id.in_(missing_import_ids),
                    ImportJob.status == IMPORT_RUNNING,
                )
                .order_by(ImportJob.id)
                .with_for_update(
                    of=(ImportJob, DownloadJob),
                    skip_locked=True,
                )
            )).all())

        task_service = TaskService(self.db)
        notifications: list[tuple[str, str, str, str]] = []
        subscription_ids: set[UUID] = set()
        stale_count = 0

        async def update_task_projection(
            task: TaskRun | None,
            job: DownloadJob | ImportJob,
            *,
            subject_type: str,
            error: str,
        ) -> None:
            if task is None:
                if subject_type == "download_job":
                    await task_service.ensure_download_task(job)
                else:
                    await task_service.ensure_import_task(job)
                return
            await task_service.update_task(
                task,
                status="stale",
                error=error,
                resource_state="yielded",
                resource_reason=None,
            )

        for job, task in locked_download_rows:
            old_status = job.status
            error = "Redis heartbeat TTL expired while download was running"
            transition_download_job(job, "stale", error)
            append_manifest_event(
                job,
                "status_changed",
                from_status=old_status,
                to_status="stale",
                reason="heartbeat_ttl_expired",
            )
            await update_task_projection(
                task,
                job,
                subject_type="download_job",
                error=error,
            )
            if job.subscription_id:
                subscription_ids.add(job.subscription_id)
            notifications.append((str(job.id), "download", old_status, "stale"))
            stale_count += 1

        for import_job, import_task_row, parent, parent_task in locked_import_rows:
            old_import_status = import_job.status
            import_error = "Redis heartbeat TTL expired while import was running"
            transition_import_job(import_job, IMPORT_STALE, import_error)
            await update_task_projection(
                import_task_row,
                import_job,
                subject_type="import_job",
                error=import_error,
            )
            notifications.append((
                str(import_job.id),
                "import",
                old_import_status,
                IMPORT_STALE,
            ))
            stale_count += 1

            # The parent's ``importing`` state represents this exact child
            # worker.  Transition both projections together so retries never
            # leave the subscription view permanently "importing".
            if parent.status == "importing":
                old_parent_status = parent.status
                parent_error = (
                    "Import worker heartbeat TTL expired "
                    f"(import_job={import_job.id})"
                )
                transition_download_job(parent, "stale", parent_error)
                append_manifest_event(
                    parent,
                    "status_changed",
                    from_status=old_parent_status,
                    to_status="stale",
                    reason="import_heartbeat_ttl_expired",
                    import_job_id=str(import_job.id),
                )
                await update_task_projection(
                    parent_task,
                    parent,
                    subject_type="download_job",
                    error=parent_error,
                )
                if parent.subscription_id:
                    subscription_ids.add(parent.subscription_id)
                notifications.append((
                    str(parent.id),
                    "download",
                    old_parent_status,
                    "stale",
                ))
                stale_count += 1

        if not stale_count:
            return 0

        await request_search_projection(
            self.db,
            subscription_ids=subscription_ids,
        )
        await self.db.commit()

        # Publish only after the domain rows, TaskRuns, and search outbox are
        # atomically committed.  Pub/sub is best effort and never rolls state
        # back or creates a second stale transition.
        for task_id, task_type, old_status, new_status in notifications:
            try:
                TaskEventPublisher.publish_status_change(
                    task_id,
                    task_type,
                    old_status,
                    new_status,
                )
            except Exception:
                logger.debug(
                    "Unable to publish stale task transition %s",
                    task_id,
                    exc_info=True,
                )
        logger.warning(
            "Marked %d domain tasks stale (Redis heartbeat TTL expired)",
            stale_count,
        )
        return stale_count

    # ── Batch by filter ───────────────────────

    async def batch_by_filter(
        self,
        task_type: str,
        filters: dict[str, Any],
        action: str,
        *,
        operator: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Apply an action to all tasks matching the given filters.

        ``filters`` may contain: ``source``, ``status``, ``subscription_id``, ``ids``.
        ``action`` must be: ``retry``, ``pause``, ``resume``, ``cancel``, or ``delete``.
        """
        valid_actions = {"retry", "pause", "resume", "cancel", "delete"}
        if action not in valid_actions:
            raise TaskEngineError(f"Invalid batch action '{action}'.")

        if task_type == "download":
            model = DownloadJob
            stmt = select(DownloadJob)
        elif task_type == "import":
            model = ImportJob
            stmt = select(ImportJob)
        else:
            raise TaskEngineError(f"Invalid task_type '{task_type}'.")

        if filters.get("ids"):
            ids = [UUID(i) for i in filters["ids"]]
            stmt = stmt.where(model.id.in_(ids))
        if filters.get("status"):
            stmt = stmt.where(model.status == filters["status"])
        if filters.get("source") and task_type == "download":
            stmt = stmt.where(model.source == filters["source"])
        if filters.get("subscription_id") and task_type == "download":
            stmt = stmt.where(model.subscription_id == filters["subscription_id"])

        result = await self.db.execute(stmt)
        tasks = list(result.scalars().all())

        results: dict[str, Any] = {
            "action": action,
            "task_type": task_type,
            "filters": filters,
            "total_matched": len(tasks),
            "succeeded": 0,
            "failed": 0,
            "errors": [],
        }

        # Map task_type+action → handler
        handlers: dict[str, dict[str, Any]] = {
            "download": {
                "retry": self.retry_download,
                "pause": self.pause_download,
                "resume": self.resume_download,
                "cancel": self.cancel_download,
                "delete": self.delete_download,
            },
            "import": {
                "retry": self.retry_import,
                "pause": self.pause_import,
                "resume": self.resume_import,
                "cancel": self.cancel_import,
                "delete": self.delete_import,
            },
        }
        handler = handlers[task_type][action]

        for task in tasks:
            task_id = task.id
            try:
                kwargs: dict[str, Any] = {"operator": operator}
                if action in {"pause", "cancel"} and note:
                    kwargs["note"] = note
                if action == "delete":
                    await handler(task_id)
                else:
                    result_item = await handler(task_id, **kwargs)
                    # handler returns dict for non-delete, None for delete
                    if result_item:
                        pass
                results["succeeded"] += 1
            except Exception as exc:
                results["failed"] += 1
                results["errors"].append({"id": str(task_id), "error": str(exc)})

        await self.db.commit()
        return results

    # ── Internal helpers ──────────────────────

    async def _apply_transition(
        self,
        job: DownloadJob,
        target_status: str,
        *,
        operator: str | None = None,
        note: str | None = None,
    ) -> DownloadJob:
        """Validate and apply a download state transition with audit fields."""
        transition_download_job(job, target_status)
        if note:
            job.user_note = note
        if operator:
            job.operator_name = operator
            job.operator_action = target_status
        await request_search_projection(
            self.db,
            subscription_ids=[job.subscription_id] if job.subscription_id else (),
        )
        if hasattr(self.db, "execute"):
            task_service = TaskService(self.db)
            task = await task_service.get_by_subject("download_job", job.id)
            if task is None:
                await task_service.ensure_download_task(job)
            else:
                await task_service.update_task(
                    task,
                    status=target_status,
                    progress=(job.progress_data if isinstance(job.progress_data, dict) else None),
                    error=job.error_log,
                )
            if target_status in {"failed", "stale", "cancelled"}:
                from app.services.operation_attention import upsert_repository_sync_receipt

                await upsert_repository_sync_receipt(
                    self.db,
                    job,
                    status=target_status,
                )
        await self.db.flush()
        return job
