"""Single finalization seam for the download-job and TaskRun projections."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.download_lifecycle import apply_download_state, commit_download_state, lock_download
from app.services.job_manifest import append_manifest_event
from app.services.job_progress import apply_download_progress
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
    locked: bool = False,
    execution=None,
) -> dict[str, Any]:
    """Persist a coherent terminal state, commit it, then publish it."""
    if not locked:
        job = await lock_download(db, job.id, execution=execution)
        if job is None:
            raise RuntimeError("Download execution no longer owns finalization")
    if job.status == "cancelled" and status != "cancelled":
        raise RuntimeError("Cancelled download cannot be finalized by a late execution")
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
    await apply_download_state(db, job, status, error=error, receipt=False)
    append_manifest_event(job, "download_finalized", status=status, outcome=outcome)
    is_exact_remote_work = (job.manifest or {}).get("trigger") == "remote_work_import"
    if (
        status == "complete"
        and mark_synced
        and not is_exact_remote_work
        and job.subscription_source_id
    ):
        triggering_membership_id = getattr(job, "triggering_user_subscription_id", None)
        triggering_account_id = getattr(job, "triggering_remote_account_id", None)
        if triggering_membership_id is None and triggering_account_id is None:
            await mark_source_sync_success(db, job.subscription_source_id)
        else:
            await mark_source_sync_success(
                db,
                job.subscription_source_id,
                triggering_user_subscription_id=triggering_membership_id,
                triggering_remote_account_id=triggering_account_id,
                triggering_credential_generation=getattr(
                    job,
                    "triggering_credential_generation",
                    None,
                ),
            )

    # Persist the repository-owned outcome before TaskRun/DownloadJob rows
    # become eligible for asynchronous compaction.
    from app.services.operation_attention import upsert_repository_sync_receipt
    await upsert_repository_sync_receipt(db, job, status=status)

    await commit_download_state(db, job)
    return progress


async def finalize_import_parent(db: AsyncSession, completion, *, timings: dict[str, float] | None = None) -> bool:
    """Shared aggregate-to-outcome mapping for completion, close and recovery.

    The caller holds the parent/child locks. A final outcome commits here; an open
    publication only stages its projection for the caller's transaction.
    """
    from app.services.download_lifecycle import project_download_state
    from app.services.job_manifest import update_manifest
    from app.services.sync_outcome import build_sync_outcome
    job = completion.parent
    if job is None:
        return False
    stats = completion.stats
    update_manifest(job, import_stats=stats)
    if timings is not None:
        append_manifest_event(job, "import_complete", status=completion.status, **stats)
        for stage, ms in timings.items():
            append_manifest_event(job, "stage_timing", stage=stage, ms=ms)
    if not completion.should_finalize:
        await project_download_state(db, job, receipt=False)
        return False
    manifest = job.manifest or {}
    detail = manifest.get("repository_artifact_reconciliation")
    outcome = build_sync_outcome(
        "new_content" if stats["works"] > 0 else "no_changes",
        metadata_count=int(manifest["metadata_json_count"]) if manifest.get("metadata_json_count") is not None else completion.total_groups,
        media_count=int(manifest.get("image_count") or stats["assets"]),
        recovery_detail=detail if isinstance(detail, dict) else None,
    ) if completion.status == "complete" else None
    await finalize_download_job(db, job, status=completion.status, outcome=outcome,
                                error=completion.message if completion.status == "failed" else None,
                                message=completion.message, assets=stats["assets"], locked=True)
    return True
