"""Idempotent repair for legacy successful no-op downloads stored as errors."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.task_run import TaskRun
from app.services.job_progress import apply_download_progress
from app.services.sync_outcome import (
    LEGACY_NO_CHANGES_MESSAGE,
    LEGACY_ZERO_ARTIFACTS_MESSAGE,
    build_sync_outcome,
    set_download_job_outcome,
)


def _message(value: dict | None) -> str | None:
    message = (value or {}).get("message")
    return str(message) if message is not None else None


def _count(manifest: dict | None, key: str) -> int:
    try:
        return max(0, int((manifest or {}).get(key, 0)))
    except (TypeError, ValueError):
        return 0


async def repair_legacy_sync_outcomes(db: AsyncSession, *, dry_run: bool = True) -> dict[str, Any]:
    jobs = list(
        (
            await db.execute(
                select(DownloadJob)
                .where(DownloadJob.status == "complete")
                .order_by(DownloadJob.created_at.asc(), DownloadJob.id.asc())
            )
        )
        .scalars()
        .all()
    )
    job_ids = [job.id for job in jobs]
    tasks = list(
        (
            await db.execute(
                select(TaskRun).where(
                    TaskRun.subject_type == "download_job",
                    TaskRun.subject_id.in_(job_ids),
                )
            )
        )
        .scalars()
        .all()
    ) if job_ids else []
    task_by_subject = {task.subject_id: task for task in tasks}

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "matched": 0,
        "download_jobs_to_repair": 0,
        "task_runs_to_repair": 0,
        "download_jobs_repaired": 0,
        "task_runs_repaired": 0,
        "outcomes": defaultdict(int),
    }
    successful_sources: set[Any] = set()

    for job in jobs:
        task = task_by_subject.get(job.id)
        manifest = job.manifest or {}
        metadata_count = _count(manifest, "metadata_json_count")
        media_count = _count(manifest, "image_count")
        domain_legacy = (
            job.error_log == LEGACY_NO_CHANGES_MESSAGE
            or _message(job.progress_data) in {LEGACY_NO_CHANGES_MESSAGE, LEGACY_ZERO_ARTIFACTS_MESSAGE}
        )
        task_legacy = bool(
            task
            and (
                task.error_log == LEGACY_NO_CHANGES_MESSAGE
                or _message(task.progress_data) in {LEGACY_NO_CHANGES_MESSAGE, LEGACY_ZERO_ARTIFACTS_MESSAGE}
            )
        )
        is_candidate = bool(job.subscription_source_id and metadata_count == 0 and (domain_legacy or task_legacy))

        if is_candidate:
            report["matched"] += 1
            report["download_jobs_to_repair"] += 1
            if task:
                report["task_runs_to_repair"] += 1
            had_baseline = bool(manifest.get("had_sync_baseline")) or job.subscription_source_id in successful_sources
            code = "no_changes" if had_baseline else "no_content"
            report["outcomes"][code] += 1
            if not dry_run:
                outcome = build_sync_outcome(
                    code,
                    metadata_count=metadata_count,
                    media_count=media_count,
                    completed_at=job.updated_at or job.created_at,
                )
                set_download_job_outcome(job, outcome)
                job.error_log = None
                progress = apply_download_progress(
                    job,
                    "complete",
                    percent=100,
                    assets=media_count,
                    outcome_code=code,
                    publish=False,
                )
                report["download_jobs_repaired"] += 1
                if task:
                    task.status = "complete"
                    task.progress_stage = "complete"
                    task.progress_current = None
                    task.progress_total = None
                    task.progress_data = progress
                    task.result_data = outcome
                    task.error_log = None
                    report["task_runs_repaired"] += 1

        if job.subscription_source_id:
            successful_sources.add(job.subscription_source_id)

    report["outcomes"] = dict(report["outcomes"])
    if dry_run:
        await db.rollback()
    else:
        await db.commit()
    return report
