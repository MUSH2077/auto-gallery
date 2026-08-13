"""Atomic projection of an ImportJob state onto its task and parent pipeline."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_state import transition_download_job
from app.services.job_manifest import append_manifest_event
from app.services.tasks import TaskService


IMPORT_PARENT_STATUS = {
    "enqueued": "importing",
    "running": "importing",
    "recovering": "importing",
    "paused": "paused",
    "cancelled": "cancelled",
    "failed": "failed",
    "stale": "stale",
}

PARENT_TERMINAL_STATUSES = frozenset({"complete", "cancelled", "failed"})


async def project_import_pipeline_state(
    db: AsyncSession,
    import_job: ImportJob,
    *,
    status: str | None = None,
    error: str | None = None,
    result: dict[str, Any] | None = None,
    reason_code: str | None = None,
) -> DownloadJob | None:
    """Update child/parent domain and TaskRun projections in one transaction.

    Callers own the commit.  This is important: a publication failure can then
    roll back every projection together instead of leaving a false parent
    ``running`` row behind.
    """

    child_status = status or import_job.status
    task_service = TaskService(db)
    child_task = await task_service.get_by_subject("import_job", import_job.id)
    if child_task is None:
        child_task = await task_service.ensure_import_task(import_job)
    else:
        child_kwargs: dict[str, Any] = {}
        if reason_code is not None:
            child_kwargs["reason_code"] = reason_code
        await task_service.update_task(
            child_task,
            status=child_status,
            progress=(
                import_job.progress_data
                if isinstance(import_job.progress_data, dict)
                else None
            ),
            result=result,
            error=error,
            **child_kwargs,
        )

    parent = await db.get(DownloadJob, import_job.download_job_id)
    if parent is None:
        return None

    parent_task = await task_service.get_by_subject("download_job", parent.id)
    target_status = IMPORT_PARENT_STATUS.get(child_status)
    if target_status and parent.status != target_status:
        # Never resurrect or rewrite a previously terminal parent based on a
        # stale child process. Execution tokens and reconciliation handle that
        # race; this seam only advances the currently active pipeline.
        if parent.status not in PARENT_TERMINAL_STATUSES:
            old_parent_status = parent.status
            transition_download_job(parent, target_status, error)
            append_manifest_event(
                parent,
                "import_projection",
                from_status=old_parent_status,
                to_status=target_status,
                import_job_id=str(import_job.id),
            )
    elif error is not None and parent.status == target_status:
        parent.error_log = error

    parent_progress = dict(parent.progress_data or {})
    parent_progress.update(
        {
            "stage": "importing" if target_status == "importing" else target_status,
            "message": (import_job.progress_data or {}).get("message")
            if isinstance(import_job.progress_data, dict)
            else None,
            "import_job_id": str(import_job.id),
        }
    )
    parent.progress_data = parent_progress

    if parent_task is None:
        parent_task = await task_service.ensure_download_task(parent)
    else:
        parent_kwargs: dict[str, Any] = {}
        if reason_code is not None:
            parent_kwargs["reason_code"] = reason_code
        await task_service.update_task(
            parent_task,
            status=parent.status,
            progress=parent_progress,
            error=error,
            **parent_kwargs,
        )

    if parent.status in {"failed", "stale", "cancelled"}:
        from app.services.operation_attention import upsert_repository_sync_receipt

        await upsert_repository_sync_receipt(db, parent, status=parent.status)
    return parent
