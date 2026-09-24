"""Import execution ownership and terminal lifecycle.

This module owns the parent -> child -> task lock order, execution leases,
and final projection. The runner keeps parsing and processing work slices.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_run import TaskRun
from app.models.task_state import transition_import_job
from app.services.download_finalization import finalize_download_job
from app.services.import_dispatch import IMPORT_DISPATCH_META_KEY
from app.services.import_lifecycle import (
    coordinate_import_parent_completion,
    project_import_pipeline_state,
)
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_progress import apply_import_progress
from app.services.stage_metrics import measure_import_phase
from app.services.sync_outcome import build_sync_outcome

logger = logging.getLogger(__name__)

async def _commit_import(db: AsyncSession) -> None:
    with measure_import_phase("db_commit"):
        await db.commit()


def _import_queue_wait_seconds(job: ImportJob, dispatch: dict, now: datetime) -> float | None:
    """Measure this delivery's eligible wait, excluding a previous execution."""
    def utc(value):
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    prepared = utc(dispatch.get("prepared_at"))
    retry = bool(job.execution_attempt)
    if retry and (prepared is None or dispatch.get("claimed_at")):
        return None
    boundary = utc(dispatch.get("available_at")) or prepared
    if boundary is None and not retry:
        boundary = utc(job.created_at)
    return max(0.0, (now - boundary).total_seconds()) if boundary is not None else None


async def _claim_import_execution(
    job_uuid: UUID,
) -> tuple[ImportJob, UUID, float | None] | None:
    """Atomically claim one runnable ImportJob row for this process."""

    execution_token = uuid4()
    async with async_session() as db:
        parent_id = (
            await db.execute(
                select(ImportJob.download_job_id).where(ImportJob.id == job_uuid)
            )
        ).scalar_one_or_none()
        if parent_id is None:
            return None
        # Import execution follows the same parent -> child -> TaskRun order as
        # every projection.  The status predicate is repeated after both locks.
        await db.execute(
            select(DownloadJob.id)
            .where(DownloadJob.id == parent_id)
            .with_for_update(of=DownloadJob)
        )
        result = await db.execute(
            select(ImportJob)
            .where(
                ImportJob.id == job_uuid,
                ImportJob.status.in_(("enqueued", "pending")),
            )
            .with_for_update()
        )
        import_job = result.scalar_one_or_none()
        if import_job is None:
            return None
        task = (await db.execute(
            select(TaskRun).where(
                TaskRun.subject_type == "import_job", TaskRun.subject_id == job_uuid,
            ).with_for_update(of=TaskRun)
        )).scalar_one_or_none()
        task_meta = dict(task.meta or {}) if task is not None else {}
        dispatch = (task_meta or {}).get(IMPORT_DISPATCH_META_KEY, {})
        dispatch = dict(dispatch) if isinstance(dispatch, dict) else {}
        claimed_at = datetime.now(timezone.utc)
        queue_wait_seconds = _import_queue_wait_seconds(
            import_job, dispatch, claimed_at,
        )
        if task is not None and dispatch:
            # A Redis-only redelivery can reuse this transport attempt. Do not
            # count its previous runtime as queue latency. A newly prepared
            # durable dispatch replaces this map and establishes a new boundary.
            task_meta[IMPORT_DISPATCH_META_KEY] = {**dispatch, "claimed_at": claimed_at.isoformat()}
            task.meta = task_meta
        transition_import_job(import_job, "running")
        import_job.execution_token = execution_token
        import_job.execution_attempt = (import_job.execution_attempt or 0) + 1
        apply_import_progress(
            import_job,
            "importing",
            "Import worker started",
            current=0,
            total=import_job.progress_works_total,
        )
        await project_import_pipeline_state(
            db,
            import_job,
            status="running",
        )
        await _commit_import(db)
        return import_job, execution_token, queue_wait_seconds


async def _release_import_execution(
    job_uuid: UUID,
    execution_token: UUID,
) -> set[str]:
    """Release only leases still owned by this concrete execution."""

    from app.services.artifact_ledger import ArtifactLedger

    async with async_session() as db:
        released = await ArtifactLedger(db).release_owned_leases(
            job_uuid,
            execution_token,
        )
        await db.execute(
            update(ImportJob)
            .where(
                ImportJob.id == job_uuid,
                ImportJob.execution_token == execution_token,
            )
            .values(execution_token=None)
        )
        await _commit_import(db)
        return released


async def _owned_import_job(
    db: AsyncSession,
    job_uuid: UUID,
    execution_token: UUID,
    *,
    lock: bool = False,
) -> ImportJob | None:
    query = select(ImportJob).where(
        ImportJob.id == job_uuid,
        ImportJob.execution_token == execution_token,
    )
    if lock:
        query = query.with_for_update(of=ImportJob)
    return (
        await db.execute(query)
    ).scalar_one_or_none()


async def _complete_import_execution(job_uuid, execution_token, *, status, message, stats, total_groups, parse_ms=0, process_ms=0):
    import_job_id = str(job_uuid)
    async with async_session() as db:
        ij = await _owned_import_job(db, job_uuid, execution_token)
        if ij is None:
            raise RuntimeError("import execution ownership lost before completion")
        transition_import_job(ij, status, message if status == "failed" else None)
        if status == "complete":
            ij.error_log = None
        apply_import_progress(
            ij, status, message,
            current=stats["works"], total=total_groups, assets=stats["assets"],
        )
        if status == "failed":
            logger.warning("Import %s classified failed: %s", import_job_id, message)

        completion = await coordinate_import_parent_completion(
            db,
            ij,
            status=status,
            stats=stats,
            total_groups=total_groups,
            message=message,
        )
        # Parent and child rows are now locked in the stable order; the
        # child TaskRun projection follows before any parent finalization.
        from app.services.tasks import TaskService

        task_service = TaskService(db)
        await task_service.update_subject(
            "import_job",
            ij.id,
            status=status,
            progress=ij.progress_data,
            result={"stats": stats, "message": message}
            if status == "complete"
            else None,
            error=message if status == "failed" else None,
        )
        dj = completion.parent
        if dj:
            parent_task = await task_service.get_by_subject(
                "download_job",
                dj.id,
            )
            if parent_task is None:
                await task_service.ensure_download_task(dj)
            else:
                await task_service.update_task(
                    parent_task,
                    status=dj.status,
                    progress=dj.progress_data,
                    error=(
                        dj.error_log
                        if dj.status in {"failed", "stale"}
                        else None
                    ),
                )
            status = completion.status
            message = completion.message
            stats = completion.stats
            total_groups = completion.total_groups
            update_manifest(dj, import_stats=stats)
            append_manifest_event(dj, "import_complete", status=status, **stats)
            append_manifest_event(dj, "stage_timing", stage="parse", ms=parse_ms)
            append_manifest_event(dj, "stage_timing", stage="process", ms=process_ms)
            if not completion.should_finalize:
                await _commit_import(db)
                logger.info(
                    "Import batch %s finished; shared parent %s still has active batches",
                    import_job_id,
                    dj.id,
                )
                return
            manifest = dj.manifest or {}
            recovery_detail = manifest.get("repository_artifact_reconciliation")
            outcome = (
                build_sync_outcome(
                    "new_content" if stats["works"] > 0 else "no_changes",
                    metadata_count=(
                        int(manifest["metadata_json_count"])
                        if manifest.get("metadata_json_count") is not None
                        else total_groups
                    ),
                    media_count=int(manifest.get("image_count") or stats["assets"]),
                    recovery_detail=(
                        recovery_detail
                        if isinstance(recovery_detail, dict)
                        else None
                    ),
                )
                if status == "complete"
                else None
            )
            await finalize_download_job(
                db,
                dj,
                status=status,
                outcome=outcome,
                error=message if status == "failed" else None,
                message=message,
                assets=stats["assets"],
            )


