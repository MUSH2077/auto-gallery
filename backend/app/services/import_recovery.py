"""Conservative automatic recovery for import-side pipeline gaps."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.storage_artifact import StorageArtifact
from app.models.task_run import TaskRun
from app.services.artifact_ledger import (
    ArtifactLedger,
    downloads_artifact_predicate,
)
from app.services.import_dispatch import recover_import_dispatch_candidate
from app.services.job_manifest import append_manifest_event, get_manifest, update_manifest

logger = logging.getLogger(__name__)

BOUNDED_RECOVERY_PAGE_SIZE = 25
ACTIVE_PUBLISHER_TASK_STATUSES = frozenset({
    "enqueued",
    "running",
    "recovering",
    "paused",
})


def _recoverable_unassigned_metadata(parent_id: UUID, now: datetime):
    return and_(
        downloads_artifact_predicate(),
        StorageArtifact.download_job_id == parent_id,
        StorageArtifact.artifact_type == "metadata_json",
        StorageArtifact.import_job_id.is_(None),
        or_(
            StorageArtifact.state.in_(("new", "failed")),
            and_(
                StorageArtifact.state == "importing",
                or_(
                    StorageArtifact.lease_expires_at.is_(None),
                    StorageArtifact.lease_expires_at <= now,
                ),
            ),
        ),
    )


async def _publisher_task_is_active(
    db: AsyncSession,
    parent: DownloadJob,
) -> bool:
    raw_task_id = get_manifest(parent).get("bounded_import_publisher_task_id")
    if not raw_task_id:
        return False
    try:
        task_id = UUID(str(raw_task_id))
    except (TypeError, ValueError):
        return False
    task = await db.get(TaskRun, task_id)
    return bool(task and task.status in ACTIVE_PUBLISHER_TASK_STATUSES)


async def _finalize_bounded_recovery_completion(db, completion) -> None:
    from app.services.download_finalization import finalize_download_job
    from app.services.sync_outcome import build_sync_outcome

    outcome = (
        build_sync_outcome(
            "new_content" if completion.stats["works"] > 0 else "no_changes",
            metadata_count=completion.total_groups,
            media_count=completion.stats["assets"],
        )
        if completion.status == "complete"
        else None
    )
    await finalize_download_job(
        db,
        completion.parent,
        status=completion.status,
        outcome=outcome,
        error=completion.message if completion.status == "failed" else None,
        message=completion.message,
        assets=completion.stats["assets"],
    )


async def recover_import_pipeline(
    db: AsyncSession,
    *,
    redis_client=None,
    stale_after_seconds: int = 900,
    dispatch_grace_seconds: int = 30,
    limit: int = 20,
) -> dict:
    """Repair safe import gaps and return a summary.

    Safe actions only:
    - replay old enqueued imports whose deterministic RQ publication is
      missing, while leaving Redis capacity failures pending;
    - enqueue an import for downloaded jobs that already have pending metadata
      in the durable artifact ledger and no import job;
    - resume one bounded page for a stale disk publisher, then close it once no
      unassigned page remains.

    Committed works with a missing library ``metadata.json`` are intentionally
    not replayed here.  Their independent ``ImportCurationOutbox.metadata_*``
    lease is counted and woken by the outbox coordinator, so recovery never
    decodes media or re-enqueues search/Gitllery projection for an existing
    work.

    Running-task liveness is deliberately outside this recovery path.
    ``TaskEngine.detect_stale_tasks`` owns the Redis TTL check and updates the
    ImportJob, parent DownloadJob, TaskRuns, and search outbox atomically.
    """

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max(stale_after_seconds, 1))
    dispatch_cutoff = now - timedelta(seconds=max(dispatch_grace_seconds, 1))
    result = {
        "imports_enqueued": 0,
        "imports_replayed": 0,
        "imports_existing": 0,
        "imports_deferred": 0,
        "imports_invalid": 0,
        "download_job_ids": [],
        "import_job_ids": [],
    }

    # ImportJob/TaskRun form a lightweight publication outbox.  Process only a
    # small oldest-first batch; the common publisher locks each job row and
    # resolves its deterministic RQ id before replaying it.
    enqueued_import_ids = list((await db.execute(
        select(ImportJob.id)
        .outerjoin(
            TaskRun,
            (TaskRun.subject_type == "import_job")
            & (TaskRun.subject_id == ImportJob.id),
        )
        .where(
            ImportJob.status == "enqueued",
            func.coalesce(TaskRun.updated_at, ImportJob.updated_at) < dispatch_cutoff,
        )
        .order_by(
            func.coalesce(TaskRun.updated_at, ImportJob.updated_at).asc(),
            ImportJob.id.asc(),
        )
        .limit(limit)
    )).scalars())
    for import_job_id in enqueued_import_ids:
        outcome = await recover_import_dispatch_candidate(
            db,
            import_job_id,
            redis_client=redis_client,
        )
        counter = f"imports_{outcome}"
        if counter in result:
            result[counter] += 1
        if outcome == "replayed":
            result["imports_enqueued"] += 1
            result["import_job_ids"].append(str(import_job_id))

    candidates = list((await db.execute(
        select(DownloadJob)
        .where(
            DownloadJob.status == "downloaded",
            DownloadJob.updated_at < cutoff,
            DownloadJob.manifest["bounded_import_publication_open"]
            .as_boolean()
            .is_not(True),
            exists().where(
                downloads_artifact_predicate(),
                StorageArtifact.download_job_id == DownloadJob.id,
                StorageArtifact.artifact_type == "metadata_json",
                StorageArtifact.state.in_(("new", "importing")),
            ),
            ~exists().where(ImportJob.download_job_id == DownloadJob.id),
        )
        .order_by(DownloadJob.updated_at.asc())
        .limit(limit)
    )).scalars())

    if candidates:
        from app.jobs.download import _enqueue_import
        for job in candidates:
            metadata_count, _image_count, paths = await ArtifactLedger(db).counts(job.id)
            if metadata_count <= 0:
                continue
            import_job_id = await _enqueue_import(
                str(job.id),
                import_error=f"auto recovery after downloaded state gap ({metadata_count} metadata files)",
                new_json_paths=set(paths),
            )
            if import_job_id:
                result["imports_enqueued"] += 1
                result["download_job_ids"].append(str(job.id))
                result["import_job_ids"].append(str(import_job_id))

    # A bounded disk publisher owns at most one adopted 25-work page between
    # durable commits. If its task disappeared, reserve the stale parent under
    # the common artifact -> owner lock order, publish that exact page, and
    # close only when no further unassigned row remains. A fresh claim advances
    # updated_at, making another recovery pass idempotent even if this process
    # dies before publication.
    bounded_parent_ids = list((await db.execute(
        select(DownloadJob.id)
        .where(
            DownloadJob.status.in_(("downloaded", "importing", "failed")),
            DownloadJob.updated_at < cutoff,
            DownloadJob.manifest["disk_import_recovery"].as_boolean().is_(True),
            DownloadJob.manifest["bounded_import_publication_open"]
            .as_boolean()
            .is_(True),
        )
        .order_by(DownloadJob.updated_at.asc(), DownloadJob.id.asc())
        .limit(limit)
    )).scalars())

    if bounded_parent_ids:
        from app.jobs.download import _enqueue_import_from_locked_page
        from app.services.import_lifecycle import close_bounded_import_publication

        for parent_id in bounded_parent_ids:
            artifact_rows = list((await db.execute(
                select(StorageArtifact)
                .where(
                    _recoverable_unassigned_metadata(parent_id, now),
                )
                .order_by(
                    StorageArtifact.source_work_id.asc(),
                    StorageArtifact.created_at.asc(),
                    StorageArtifact.id.asc(),
                )
                .limit(BOUNDED_RECOVERY_PAGE_SIZE + 1)
                .with_for_update(of=StorageArtifact, skip_locked=True)
            )).scalars())
            parent = (
                await db.execute(
                    select(DownloadJob)
                    .where(DownloadJob.id == parent_id)
                    .with_for_update(of=DownloadJob)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if parent is None:
                await db.rollback()
                continue
            manifest = get_manifest(parent)
            if (
                not manifest.get("disk_import_recovery")
                or not manifest.get("bounded_import_publication_open")
                or parent.updated_at >= cutoff
                or await _publisher_task_is_active(db, parent)
            ):
                await db.rollback()
                continue

            page_rows = artifact_rows[:BOUNDED_RECOVERY_PAGE_SIZE]
            page_ids = [row.id for row in page_rows]
            outstanding = bool((await db.execute(
                select(exists().where(
                    _recoverable_unassigned_metadata(parent_id, now),
                    *(
                        (StorageArtifact.id.not_in(page_ids),)
                        if page_ids
                        else ()
                    ),
                ))
            )).scalar_one())
            if not page_rows and outstanding:
                # A concurrent recovery worker owns the artifact lock. Do not
                # reverse the lock order or mistake SKIP LOCKED for an empty
                # publisher; a later pass will observe its committed outcome.
                await db.rollback()
                continue
            has_more = outstanding
            claim_id = str(uuid4())
            update_manifest(
                parent,
                bounded_import_recovery_claim=claim_id,
                bounded_import_recovery_claimed_at=now.isoformat(),
            )
            append_manifest_event(
                parent,
                "bounded_import_recovery_claimed",
                claim_id=claim_id,
                paths=len(page_rows),
                has_more=has_more,
            )

            import_job_id = None
            if page_rows:
                try:
                    import_job_id = await _enqueue_import_from_locked_page(
                        db,
                        parent_id,
                        artifact_ids=set(page_ids),
                        import_error="auto recovery after interrupted bounded publication",
                    )
                except Exception:
                    await db.rollback()
                    logger.warning(
                        "bounded import publisher recovery failed for %s",
                        parent_id,
                        exc_info=True,
                    )
                    continue
                if import_job_id is None:
                    continue
                result["imports_enqueued"] += 1
                result["download_job_ids"].append(str(parent_id))
                result["import_job_ids"].append(str(import_job_id))

            if has_more:
                parent = (
                    await db.execute(
                        select(DownloadJob)
                        .where(DownloadJob.id == parent_id)
                        .with_for_update(of=DownloadJob)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                update_manifest(
                    parent,
                    bounded_import_recovery_claim=None,
                    bounded_import_recovery_claimed_at=None,
                )
                await db.commit()
                continue

            completion = await close_bounded_import_publication(db, parent_id)
            if completion is not None and completion.should_finalize:
                await _finalize_bounded_recovery_completion(db, completion)
            else:
                await db.commit()

    if result["imports_enqueued"]:
        await db.commit()
    return result
