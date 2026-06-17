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
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
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
    IMPORT_PAUSABLE_STATUSES,
    IMPORT_CANCELLABLE_STATUSES,
    IMPORT_FAILED,
    IMPORT_STALE,
    transition_download_job,
    transition_import_job,
)
from app.repositories.download_job import DownloadJobRepository
from app.services.job_manifest import append_manifest_event
from app.services.redis_client import get_redis
from app.services.redis_pubsub import TaskChannel, TaskEventPublisher

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200
HEARTBEAT_TIMEOUT = 60


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

    # ── Pause ─────────────────────────────────

    async def pause_download(
        self, job_id: UUID, *, note: str | None = None, operator: str | None = None,
    ) -> dict[str, Any]:
        """Pause a download job. Sends SIGTERM signal to the running process group."""
        job = await self._get_download(job_id)

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
        await self.db.flush()

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

        if job.status != DOWNLOAD_PAUSED:
            raise TaskEngineError(
                f"Cannot resume download job with status '{job.status}'. Must be 'paused'."
            )

        old_status = job.status
        job = await self._apply_transition(job, DOWNLOAD_ENQUEUED, operator=operator)

        self._enqueue_rq("downloads", "app.jobs.download.run_download_job", str(job_id))
        append_manifest_event(job, "enqueued", queue="downloads", action="resume")
        await self.db.flush()

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
        await self.db.flush()

        self._enqueue_rq("imports", "app.jobs.import_runner.run_import_job", str(job_id))

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
        await self.db.flush()

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

        if job.status not in DOWNLOAD_RETRYABLE_STATUSES:
            raise TaskEngineError(
                f"Cannot retry download job with status '{job.status}'. "
                f"Allowed: {sorted(DOWNLOAD_RETRYABLE_STATUSES)}"
            )

        old_status = job.status
        job = await self._apply_transition(job, DOWNLOAD_ENQUEUED, operator=operator)
        job.retry_count = 0
        job.error_log = None
        await self.db.flush()

        self._enqueue_rq("downloads", "app.jobs.download.run_download_job", str(job_id))
        append_manifest_event(job, "enqueued", queue="downloads", action="retry")
        await self.db.flush()

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
        await self.db.flush()

        self._enqueue_rq("imports", "app.jobs.import_runner.run_import_job", str(job_id))

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
        await self.db.delete(job)
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

    async def detect_stale_tasks(self) -> int:
        """Mark tasks as stale if their heartbeat has timed out."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_TIMEOUT)
        stale_count = 0

        result = await self.db.execute(
            select(DownloadJob).where(
                DownloadJob.status.in_({"downloading", "importing", "downloaded"}),
                DownloadJob.last_heartbeat_at.isnot(None),
                DownloadJob.last_heartbeat_at < cutoff,
            )
        )
        for job in result.scalars().all():
            old_status = job.status
            transition_download_job(
                job, "stale",
                f"Heartbeat lost (last: {job.last_heartbeat_at.isoformat()})",
            )
            append_manifest_event(job, "status_changed",
                                  from_status=old_status, to_status="stale",
                                  reason="heartbeat_lost")
            TaskEventPublisher.publish_status_change(
                str(job.id), "download", old_status, "stale",
            )
            stale_count += 1

        result = await self.db.execute(
            select(ImportJob).where(
                ImportJob.status.in_({IMPORT_ENQUEUED, IMPORT_RUNNING}),
                ImportJob.last_heartbeat_at.isnot(None),
                ImportJob.last_heartbeat_at < cutoff,
            )
        )
        for job in result.scalars().all():
            old_status = job.status
            job.status = "stale"
            job.error_log = f"Heartbeat lost (last: {job.last_heartbeat_at.isoformat()})"
            TaskEventPublisher.publish_status_change(
                str(job.id), "import", old_status, "stale",
            )
            stale_count += 1

        if stale_count:
            await self.db.flush()
            logger.warning("Marked %d tasks as stale (heartbeat timeout)", stale_count)

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

        ``filters`` may contain: ``source``, ``status``, ``subscription_id``.
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
        await self.db.flush()
        return job

    @staticmethod
    def _enqueue_rq(queue_name: str, func_path: str, *args: Any) -> None:
        """Enqueue a function call to an RQ queue."""
        try:
            from rq import Queue
            Queue(name=queue_name, connection=get_redis()).enqueue(
                func_path, *args, job_timeout=RQ_JOB_TIMEOUT,
            )
        except Exception:
            logger.exception("Failed to enqueue %s to %s queue", func_path, queue_name)
