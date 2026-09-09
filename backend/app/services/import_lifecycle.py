"""Atomic projection of an ImportJob state onto its task and parent pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import isawaitable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_state import transition_download_job
from app.services.job_manifest import append_manifest_event, get_manifest, update_manifest
from app.services.tasks import TaskService
from app.services.download_failure_evidence import unresolved_provider_failure


# Stable lifecycle lock order across workers, recovery, retries, and projection:
# downloads StorageArtifact rows (when present) -> DownloadJob -> ImportJob ->
# TaskRun.  Callers that do not participate in an earlier relation begin at the
# next relation and never acquire an earlier lock afterward.

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
BOUNDED_SETTLED_STATUSES = frozenset({
    "complete",
    "cancelled",
    "failed",
    "stale",
})


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
    manifest = get_manifest(parent)
    publication_open = bool(manifest.get("bounded_import_publication_open"))
    active = any(
        child_status not in BOUNDED_SETTLED_STATUSES
        and child_status != "paused"
        for child_status in child_statuses
    )
    paused = any(child_status == "paused" for child_status in child_statuses)
    all_settled = bool(child_statuses) and all(
        child_status in BOUNDED_SETTLED_STATUSES
        for child_status in child_statuses
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
    if publication_open or active:
        aggregate_status = "importing"
    elif paused:
        aggregate_status = "paused"
    elif any(value == "failed" for value in child_statuses):
        aggregate_status = "failed"
    elif any(value == "stale" for value in child_statuses):
        aggregate_status = "stale"
    elif any(value == "cancelled" for value in child_statuses):
        aggregate_status = "cancelled"
    elif all_settled:
        aggregate_status = "complete"
    else:
        aggregate_status = "importing"

    should_finalize = (
        all_settled
        and not publication_open
        and parent.status != aggregate_status
    )
    if aggregate_status == "complete":
        aggregate_message = (
            fallback_message
            if len(child_statuses) == 1
            else (
                f"Imported {aggregate_stats['works']} works across "
                f"{len(child_statuses)} bounded batches"
            )
        )
    elif aggregate_status == "cancelled":
        aggregate_message = "One or more bounded import batches were cancelled"
    elif aggregate_status == "failed":
        aggregate_message = "One or more bounded import batches failed"
    elif aggregate_status == "stale":
        aggregate_message = "One or more bounded import batches lost its worker"
    elif aggregate_status == "paused":
        aggregate_message = "All active bounded import batches are paused"
    else:
        aggregate_message = "Bounded import publication or child batches remain active"
    return ImportParentCompletion(
        parent=parent,
        should_finalize=should_finalize,
        status=aggregate_status,
        message=aggregate_message,
        stats=aggregate_stats,
        total_groups=aggregate_total,
    )


def _apply_bounded_parent_aggregate(
    parent: DownloadJob,
    completion: ImportParentCompletion,
    *,
    event: str,
    import_job_id: UUID | None = None,
    error: str | None = None,
) -> None:
    """Apply an already-serialized aggregate without state-machine races."""

    if parent.status == completion.status:
        if error is not None and completion.status in {"failed", "stale"}:
            parent.error_log = error
        return
    old_parent_status = parent.status
    parent.status = completion.status
    parent.error_log = (
        error or completion.message
        if completion.status in {"failed", "stale"}
        else None
    )
    append_manifest_event(
        parent,
        event,
        from_status=old_parent_status,
        to_status=completion.status,
        import_job_id=str(import_job_id) if import_job_id else None,
    )


async def _lock_parent(
    db: AsyncSession,
    download_job_id: UUID,
) -> DownloadJob | None:
    """Lock the shared parent without autoflushing a dirty child first."""

    with db.no_autoflush:
        return (
            await db.execute(
                select(DownloadJob)
                .where(DownloadJob.id == download_job_id)
                .with_for_update(of=DownloadJob)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()


async def bounded_import_parent_completion(
    db: AsyncSession,
    download_job_id: UUID,
    *,
    fallback_message: str = "Bounded import state changed",
) -> ImportParentCompletion | None:
    """Return the serialized aggregate for one bounded shared parent."""

    parent = await _lock_parent(db, download_job_id)
    if parent is None:
        return None
    manifest = get_manifest(parent)
    if not manifest.get("disk_import_recovery"):
        return None
    return await _bounded_parent_completion(
        db,
        parent,
        batch_results=dict(manifest.get("bounded_import_batches") or {}),
        fallback_message=fallback_message,
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

    parent = await _lock_parent(db, import_job.download_job_id)
    if parent is None:
        raise RuntimeError(
            f"download job {import_job.download_job_id} disappeared during import",
        )
    # Stable lifecycle order: parent -> child -> TaskRun.  The parent query
    # deliberately ran under ``no_autoflush`` so a dirty child could not take
    # its row lock first.
    with db.no_autoflush:
        await db.execute(
            select(ImportJob.id)
            .where(ImportJob.id == import_job.id)
            .with_for_update(of=ImportJob)
        )
    await db.flush([import_job])
    manifest = get_manifest(parent)
    if not manifest.get("disk_import_recovery"):
        provider_failure = unresolved_provider_failure(parent)
        if status == "complete" and provider_failure is not None:
            should_finalize = parent.status not in PARENT_TERMINAL_STATUSES
            if should_finalize:
                transition_download_job(
                    parent,
                    "failed",
                    provider_failure["reason"],
                )
            return ImportParentCompletion(
                parent=parent,
                should_finalize=should_finalize,
                status="failed",
                message=provider_failure["reason"],
                stats=dict(stats),
                total_groups=int(total_groups),
            )
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
    completion = await _bounded_parent_completion(
        db,
        parent,
        batch_results=batch_results,
        fallback_message=message,
    )
    if not completion.should_finalize:
        _apply_bounded_parent_aggregate(
            parent,
            completion,
            event="import_aggregate",
            import_job_id=import_job.id,
        )
    return completion


async def close_bounded_import_publication(
    db: AsyncSession,
    download_job_id: UUID,
    *,
    publisher_checkpoint=None,
) -> ImportParentCompletion | None:
    """Close child publication and elect a finalizer if all children finished."""

    parent = await _lock_parent(db, download_job_id)
    if parent is None:
        return None
    manifest = get_manifest(parent)
    if not manifest.get("disk_import_recovery"):
        return None
    if publisher_checkpoint is not None:
        checkpoint = publisher_checkpoint(db, lock_task=True)
        if isawaitable(checkpoint):
            await checkpoint
    batch_results = dict(manifest.get("bounded_import_batches") or {})
    update_manifest(
        parent,
        bounded_import_publication_open=False,
        bounded_import_recovery_claim=None,
        bounded_import_recovery_claimed_at=None,
    )
    await db.flush()
    completion = await _bounded_parent_completion(
        db,
        parent,
        batch_results=batch_results,
        fallback_message="Bounded import publication complete",
    )
    if not completion.should_finalize:
        _apply_bounded_parent_aggregate(
            parent,
            completion,
            event="bounded_import_publication_closed",
        )
    return completion


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
    parent = await _lock_parent(db, import_job.download_job_id)
    if parent is None:
        return None

    with db.no_autoflush:
        await db.execute(
            select(ImportJob.id)
            .where(ImportJob.id == import_job.id)
            .with_for_update(of=ImportJob)
        )
    await db.flush([import_job])

    task_service = TaskService(db)
    child_task = await task_service.get_by_subject("import_job", import_job.id)
    if child_task is None:
        child_task = await task_service.ensure_import_task(import_job)
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

    parent_task = await task_service.get_by_subject("download_job", parent.id)
    manifest = get_manifest(parent)
    bounded = bool(manifest.get("disk_import_recovery"))
    if bounded:
        batch_results = dict(manifest.get("bounded_import_batches") or {})
        result_data = result if isinstance(result, dict) else {}
        result_stats = result_data.get("stats")
        batch_results[str(import_job.id)] = {
            "status": child_status,
            "stats": dict(result_stats) if isinstance(result_stats, dict) else {},
            "total_groups": int(result_data.get("total_groups") or 0),
            "message": error or (
                (import_job.progress_data or {}).get("message")
                if isinstance(import_job.progress_data, dict)
                else None
            ),
        }
        update_manifest(parent, bounded_import_batches=batch_results)
        await db.flush()
        completion = await _bounded_parent_completion(
            db,
            parent,
            batch_results=batch_results,
            fallback_message=error or "Bounded import state changed",
        )
        target_status = completion.status
    else:
        target_status = IMPORT_PARENT_STATUS.get(child_status)

    if target_status and parent.status != target_status:
        old_parent_status = parent.status
        if bounded:
            # Bounded parents are an aggregate, not an independent terminal
            # latch.  A retried/active child reopens a failed or stale parent;
            # a closed settled child set deterministically elects the terminal
            # status above.
            _apply_bounded_parent_aggregate(
                parent,
                completion,
                event="import_projection",
                import_job_id=import_job.id,
                error=error,
            )
        # Ordinary one-child parents retain their terminal guard. Execution
        # tokens and reconciliation handle a stale child process race.
        elif parent.status not in PARENT_TERMINAL_STATUSES:
            transition_download_job(parent, target_status, error)
            append_manifest_event(
                parent,
                "import_projection",
                from_status=old_parent_status,
                to_status=target_status,
                import_job_id=str(import_job.id),
            )
    elif (
        error is not None
        and parent.status == target_status
        and target_status in {"failed", "stale"}
    ):
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
        if reason_code is not None and parent.status in {"failed", "stale"}:
            parent_kwargs["reason_code"] = reason_code
        await task_service.update_task(
            parent_task,
            status=parent.status,
            progress=parent_progress,
            error=(
                error
                if parent.status in {"failed", "stale"}
                else None
            ),
            **parent_kwargs,
        )

    if parent.status in {"failed", "stale", "cancelled"}:
        from app.services.operation_attention import upsert_repository_sync_receipt

        await upsert_repository_sync_receipt(db, parent, status=parent.status)
    return parent
