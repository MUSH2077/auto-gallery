"""Shared progress helpers for download and import jobs."""

from __future__ import annotations

from typing import Any

from app.services.progress import ProgressTracker
from app.services.redis_pubsub import TaskEventPublisher


def make_progress(
    stage: str,
    message: str | None = None,
    *,
    current: int | None = None,
    total: int | None = None,
    percent: float | None = None,
    assets: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    progress: dict[str, Any] = {"stage": stage}
    if message:
        progress["message"] = message
    if current is not None:
        progress["current"] = current
    if total is not None:
        progress["total"] = total
    if percent is not None:
        progress["percent"] = percent
    elif current is not None and total:
        progress["percent"] = round(current / total * 100, 1)
    if assets is not None:
        progress["assets"] = assets
    progress.update({k: v for k, v in extra.items() if v is not None})
    return progress


def publish_progress(task_id: str, task_type: str, progress: dict[str, Any]) -> None:
    """Publish progress for WebSocket and short-lived polling snapshots."""
    ProgressTracker.set(task_id, progress)
    TaskEventPublisher.publish_progress(task_id, task_type, progress)


def apply_download_progress(
    job,
    stage: str,
    message: str | None = None,
    *,
    current: int | None = None,
    total: int | None = None,
    percent: float | None = None,
    assets: int | None = None,
    publish: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    progress = make_progress(
        stage,
        message,
        current=current,
        total=total,
        percent=percent,
        assets=assets,
        **extra,
    )
    job.pipeline_stage = stage
    job.progress_data = progress
    if publish:
        publish_progress(str(job.id), "download", progress)
    return progress


def apply_import_progress(
    job,
    stage: str,
    message: str | None = None,
    *,
    current: int | None = None,
    total: int | None = None,
    percent: float | None = None,
    assets: int | None = None,
    publish: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    progress = make_progress(
        stage,
        message,
        current=current,
        total=total,
        percent=percent,
        assets=assets,
        **extra,
    )
    job.progress_stage = stage
    if current is not None:
        job.progress_works_done = current
    if total is not None:
        job.progress_works_total = total
    job.progress_data = progress
    if publish:
        publish_progress(str(job.id), "import", progress)
    return progress


def import_progress_from_job(job) -> dict[str, Any] | None:
    if getattr(job, "progress_data", None):
        return job.progress_data
    stage = getattr(job, "progress_stage", None)
    if not stage:
        return None
    current = getattr(job, "progress_works_done", None)
    total = getattr(job, "progress_works_total", None)
    return make_progress(stage, current=current, total=total)
