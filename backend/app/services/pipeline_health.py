"""Read-only health checks for the download -> import pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.storage_artifact import StorageArtifact


def _age_cutoff(stale_after_seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=max(stale_after_seconds, 1))


def _redis_exists(redis_client: Any | None, key: str) -> bool:
    if redis_client is None:
        return False
    try:
        return bool(redis_client.exists(key))
    except Exception:
        return False


async def collect_pipeline_health(
    db: AsyncSession,
    *,
    redis_client: Any | None = None,
    stale_after_seconds: int = 900,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Return a compact, read-only snapshot of pipeline health.

    The checks focus on states that strand data after it has reached disk:
    downloaded jobs without an import job, import jobs that lost heartbeat, and
    old queued imports. The result is JSON-serializable and suitable for ops
    scripts.
    """

    cutoff = _age_cutoff(stale_after_seconds)

    download_status_rows = await db.execute(
        select(DownloadJob.status, func.count(DownloadJob.id))
        .group_by(DownloadJob.status)
        .order_by(DownloadJob.status)
    )
    import_status_rows = await db.execute(
        select(ImportJob.status, func.count(ImportJob.id))
        .group_by(ImportJob.status)
        .order_by(ImportJob.status)
    )
    artifact_state_rows = await db.execute(
        select(StorageArtifact.state, StorageArtifact.artifact_type, func.count(StorageArtifact.id))
        .group_by(StorageArtifact.state, StorageArtifact.artifact_type)
        .order_by(StorageArtifact.state, StorageArtifact.artifact_type)
    )

    no_import_stmt = (
        select(DownloadJob)
        .where(
            DownloadJob.status == "downloaded",
            DownloadJob.created_at < cutoff,
            ~exists().where(ImportJob.download_job_id == DownloadJob.id),
        )
        .order_by(DownloadJob.created_at)
        .limit(sample_limit)
    )
    downloaded_without_import = list((await db.execute(no_import_stmt)).scalars())

    pending_artifact_stmt = (
        select(DownloadJob)
        .where(
            DownloadJob.status == "downloaded",
            DownloadJob.created_at < cutoff,
            exists().where(
                StorageArtifact.download_job_id == DownloadJob.id,
                StorageArtifact.artifact_type == "metadata_json",
                StorageArtifact.state.in_(("new", "importing")),
            ),
            ~exists().where(ImportJob.download_job_id == DownloadJob.id),
        )
        .order_by(DownloadJob.created_at)
        .limit(sample_limit)
    )
    downloaded_with_pending_artifacts = list((await db.execute(pending_artifact_stmt)).scalars())

    old_enqueued_imports = list((await db.execute(
        select(ImportJob)
        .where(ImportJob.status == "enqueued", ImportJob.created_at < cutoff)
        .order_by(ImportJob.created_at)
        .limit(sample_limit)
    )).scalars())

    running_imports = list((await db.execute(
        select(ImportJob)
        .where(ImportJob.status == "running", ImportJob.created_at < cutoff)
        .order_by(ImportJob.created_at)
        .limit(sample_limit)
    )).scalars())
    importing_without_heartbeat = [
        job for job in running_imports
        if not _redis_exists(redis_client, f"task:{job.id}:heartbeat_ts")
    ]

    def job_summary(job: DownloadJob | ImportJob) -> dict[str, Any]:
        return {
            "id": str(job.id),
            "status": job.status,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }

    stuck = {
        "downloaded_without_import": [job_summary(job) for job in downloaded_without_import],
        "downloaded_with_pending_artifacts": [job_summary(job) for job in downloaded_with_pending_artifacts],
        "old_enqueued_imports": [job_summary(job) for job in old_enqueued_imports],
        "importing_without_heartbeat": [job_summary(job) for job in importing_without_heartbeat],
    }

    return {
        "ok": not any(stuck.values()),
        "stale_after_seconds": stale_after_seconds,
        "download_statuses": {status: int(count) for status, count in download_status_rows.all()},
        "import_statuses": {status: int(count) for status, count in import_status_rows.all()},
        "artifact_states": [
            {"state": state, "artifact_type": artifact_type, "count": int(count)}
            for state, artifact_type, count in artifact_state_rows.all()
        ],
        "stuck": stuck,
    }
