"""Atomic projection of an ImportJob state onto its task and parent pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_state import IMPORT_TERMINAL_STATUSES, transition_download_job
from app.services.job_manifest import append_manifest_event, get_manifest, update_manifest
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


@dataclass(frozen=True, slots=True)
class ImportParentCompletion:
    parent: DownloadJob
    should_finalize: bool
    status: str
    message: str
    stats: dict[str, int]
    total_groups: int


async def _bounded_parent_completion(
    db: AsyncSession,
    parent: DownloadJob,
    *,
    batch_results: dict,
    fallback_message: str,
) -> ImportParentCompletion:
    child_statuses = list((await db.execute(
        select(ImportJob.status)
        .where(ImportJob.download_job_id == parent.id)
        .order_by(ImportJob.created_at, ImportJob.id)
    )).scalars())
    pending = any(
        child_status not in IMPORT_TERMINAL_STATUSES
        for child_status in child_statuses
    )
    manifest = get_manifest(parent)
    publication_open = bool(manifest.get("bounded_import_publication_open"))
    parent_is_terminal = parent.status in PARENT_TERMINAL_STATUSES
    should_finalize = (
        bool(child_statuses)
        and not publication_open
        and not pending
        and not parent_is_terminal
    )

    aggregate_stats = {
        key: sum(
            int((result.get("stats") or {}).get(key) or 0)
            for result in batch_results.values()
        )
        for key in ("works", "assets", "multi_page", "skipped", "existing")
    }
    aggregate_total = sum(
        int(result.get("total_groups") or 0)
        for result in batch_results.values()
    )
    aggregate_status = (
        "complete"
        if child_statuses and all(value == "complete" for value in child_statuses)
        else "failed"
    )
    aggregate_message = (
        fallback_message
        if aggregate_status == "complete" and len(child_statuses) == 1
        else (
            f"Imported {aggregate_stats['works']} works across "
            f"{len(child_statuses)} bounded batches"
            if aggregate_status == "complete"
            else "One or more bounded import batches failed"
        )
    )
    return ImportParentCompletion(
        parent=parent,
        should_finalize=should_finalize,
        status=aggregate_status,
        message=aggregate_message,
        stats=aggregate_stats,
        total_groups=aggregate_total,
    )


async def coordinate_import_parent_completion(
    db: AsyncSession,
    import_job: ImportJob,
    *,
    status: str,
    stats: dict[str, int],
    total_groups: int,
    message: str,
) -> ImportParentCompletion:
    """Serialize bounded child results before terminalizing their one parent."""

    parent = (
        await db.execute(
            select(DownloadJob)
            .where(DownloadJob.id == import_job.download_job_id)
            .with_for_update(of=DownloadJob)
        )
    ).scalar_one()
    manifest = get_manifest(parent)
    if not manifest.get("disk_import_recovery"):
        return ImportParentCompletion(
            parent=parent,
            should_finalize=parent.status not in PARENT_TERMINAL_STATUSES,
            status=status,
            message=message,
            stats=dict(stats),
            total_groups=int(total_groups),
        )

    batch_results = dict(manifest.get("bounded_import_batches") or {})
    batch_results[str(import_job.id)] = {
        "status": status,
        "stats": stats,
        "total_groups": int(total_groups),
        "message": message,
    }
    update_manifest(parent, bounded_import_batches=batch_results)
    await db.flush()
    return await _bounded_parent_completion(
        db,
        parent,
        batch_results=batch_results,
        fallback_message=message,
    )


async def close_bounded_import_publication(
    db: AsyncSession,
    download_job_id: UUID,
) -> ImportParentCompletion | None:
    """Close child publication and elect a finalizer if all children finished."""

    parent = (
        await db.execute(
            select(DownloadJob)
            .where(DownloadJob.id == download_job_id)
            .with_for_update(of=DownloadJob)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if parent is None:
        return None
    manifest = get_manifest(parent)
    if not manifest.get("disk_import_recovery"):
        return None
    batch_results = dict(manifest.get("bounded_import_batches") or {})
    update_manifest(parent, bounded_import_publication_open=False)
    await db.flush()
    return await _bounded_parent_completion(
        db,
        parent,
        batch_results=batch_results,
        fallback_message="Bounded import publication complete",
    )


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
