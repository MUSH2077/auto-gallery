"""Conservative automatic recovery for import-side pipeline gaps."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.storage_artifact import StorageArtifact
from app.services.artifact_ledger import ArtifactLedger
from app.services.tasks import TaskService

logger = logging.getLogger(__name__)


async def recover_import_pipeline(
    db: AsyncSession,
    *,
    redis_client=None,
    stale_after_seconds: int = 900,
    limit: int = 20,
) -> dict:
    """Repair safe import gaps and return a summary.

    Safe actions only:
    - enqueue an import for downloaded jobs that already have pending metadata
      in the durable artifact ledger and no import job;
    - mark old running imports without heartbeat as stale.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(stale_after_seconds, 1))
    result = {"imports_enqueued": 0, "imports_marked_stale": 0, "download_job_ids": [], "import_job_ids": []}

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

    running_imports = list((await db.execute(
        select(ImportJob)
        .where(ImportJob.status == "running", ImportJob.updated_at < cutoff)
        .order_by(ImportJob.updated_at.asc())
        .limit(limit)
    )).scalars())
    task_svc = TaskService(db)
    for job in running_imports:
        hb_key = f"task:{job.id}:heartbeat_ts"
        has_heartbeat = False
        if redis_client is not None:
            try:
                has_heartbeat = bool(redis_client.exists(hb_key))
            except Exception:
                logger.debug("Unable to read heartbeat key %s", hb_key, exc_info=True)
        if has_heartbeat:
            continue
        job.status = "stale"
        job.error_log = (job.error_log or "") + "\n[auto] Marked stale: lost import heartbeat"
        await task_svc.update_subject(
            "import_job",
            job.id,
            status="stale",
            error=job.error_log,
        )
        result["imports_marked_stale"] += 1
        result["import_job_ids"].append(str(job.id))

    if result["imports_enqueued"] or result["imports_marked_stale"]:
        await db.commit()
    return result
