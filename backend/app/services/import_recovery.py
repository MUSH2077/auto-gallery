"""Conservative automatic recovery for import-side pipeline gaps."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.storage_artifact import StorageArtifact
from app.models.task_run import TaskRun
from app.services.artifact_ledger import ArtifactLedger
from app.services.import_dispatch import recover_import_dispatch_candidate

logger = logging.getLogger(__name__)


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
            exists().where(
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

    if result["imports_enqueued"]:
        await db.commit()
    return result
