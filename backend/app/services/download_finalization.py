"""Single finalization seam for the download-job and TaskRun projections."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.download_job import DownloadJobRepository
from app.services.job_manifest import append_manifest_event
from app.services.job_progress import apply_download_progress, publish_progress
from app.services.subscription_enqueue import mark_source_sync_success
from app.services.sync_outcome import SyncOutcome, clear_download_job_outcome, set_download_job_outcome


async def finalize_download_job(
    db: AsyncSession,
    job,
    *,
    status: str,
    outcome: SyncOutcome | None = None,
    error: str | None = None,
    message: str | None = None,
    assets: int | None = None,
    mark_synced: bool = True,
) -> dict[str, Any]:
    """Persist a coherent terminal state, commit it, then publish it."""
    if outcome is not None:
        set_download_job_outcome(job, outcome)
    elif status != "complete":
        clear_download_job_outcome(job)

    progress = apply_download_progress(
        job,
        "complete" if status == "complete" else "failed",
        message if status != "complete" else None,
        percent=100 if status == "complete" else None,
        assets=assets,
        outcome_code=outcome["code"] if outcome else None,
        publish=False,
    )
    await DownloadJobRepository(db).update_status(job, status, error)
    append_manifest_event(job, "download_finalized", status=status, outcome=outcome)
    if status == "complete" and mark_synced and job.subscription_source_id:
        await mark_source_sync_success(db, job.subscription_source_id)

    await db.commit()
    publish_progress(str(job.id), "download", progress)
    return progress
