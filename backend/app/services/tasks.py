"""Unified task-run service.

Download/import domain tables remain authoritative for their payloads. TaskRun
is the user-facing task envelope that lets operations and domain jobs share one
queue/progress surface.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_run import TaskEvent, TaskRun


NONTERMINAL_STATUSES = {"enqueued", "running", "paused", "recovering"}
TERMINAL_STATUSES = {"complete", "failed", "cancelled", "stale"}


def normalize_task_status(status: str | None) -> str:
    if not status:
        return "enqueued"
    status = status.strip().lower()
    if status in {"pending", "queued"}:
        return "enqueued"
    if status in {"downloading", "downloaded", "importing"}:
        return "running"
    return status


def deterministic_task_id(subject_type: str, subject_id: UUID | str) -> UUID:
    digest = hashlib.md5(f"{subject_type}:{subject_id}".encode("utf-8")).hexdigest()
    return UUID(f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_event(
        self,
        task: TaskRun,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            task_run_id=task.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            message=message,
            payload=payload,
            created_at=_now(),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def create_task(
        self,
        *,
        kind: str,
        operation_type: str,
        title: str,
        status: str = "enqueued",
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        parent_task_id: UUID | None = None,
        queue_name: str | None = None,
        rq_job_id: str | None = None,
        source: str | None = None,
        source_url: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
        task_id: UUID | None = None,
    ) -> TaskRun:
        if subject_type and subject_id:
            existing = await self.get_by_subject(subject_type, subject_id)
            if existing:
                await self.update_task(
                    existing,
                    status=status,
                    progress=progress,
                    result=result,
                    error=error,
                    meta={**(existing.meta or {}), **(meta or {})} if meta else existing.meta,
                    rq_job_id=rq_job_id or existing.rq_job_id,
                    parent_task_id=parent_task_id or existing.parent_task_id,
                )
                return existing
        normalized = normalize_task_status(status)
        now = _now()
        task_kwargs = {}
        if task_id or (subject_type and subject_id):
            task_kwargs["id"] = task_id or deterministic_task_id(subject_type, subject_id)
        task = TaskRun(
            **task_kwargs,
            kind=kind,
            operation_type=operation_type,
            subject_type=subject_type,
            subject_id=subject_id,
            parent_task_id=parent_task_id,
            status=normalized,
            queue_name=queue_name,
            rq_job_id=rq_job_id,
            title=title,
            source=source,
            source_url=source_url,
            progress_stage=(progress or {}).get("stage") or (progress or {}).get("phase"),
            progress_current=(progress or {}).get("current"),
            progress_total=(progress or {}).get("total"),
            progress_data=progress,
            result_data=result,
            error_log=error,
            meta=meta,
            enqueued_at=now if normalized == "enqueued" else None,
            started_at=now if normalized == "running" else None,
            finished_at=now if normalized in TERMINAL_STATUSES else None,
        )
        self.db.add(task)
        await self.db.flush()
        await self.add_event(task, "created", to_status=normalized, message=title)
        return task

    async def get_by_subject(self, subject_type: str, subject_id: UUID) -> TaskRun | None:
        result = await self.db.execute(
            select(TaskRun).where(TaskRun.subject_type == subject_type, TaskRun.subject_id == subject_id).limit(1)
        )
        return result.scalar_one_or_none()

    async def get(self, task_id: UUID) -> TaskRun | None:
        return await self.db.get(TaskRun, task_id)

    async def update_task(
        self,
        task: TaskRun,
        *,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
        rq_job_id: str | None = None,
        parent_task_id: UUID | None = None,
    ) -> TaskRun:
        old_status = task.status
        if status is not None:
            task.status = normalize_task_status(status)
            if task.status == "running" and not task.started_at:
                task.started_at = _now()
            if task.status in TERMINAL_STATUSES and not task.finished_at:
                task.finished_at = _now()
        if progress is not None:
            task.progress_data = progress
            task.progress_stage = progress.get("stage") or progress.get("phase")
            task.progress_current = progress.get("current") or progress.get("scanned")
            task.progress_total = progress.get("total")
        if result is not None:
            task.result_data = result
        if error is not None:
            task.error_log = error
        if meta is not None:
            task.meta = meta
        if rq_job_id is not None:
            task.rq_job_id = rq_job_id
        if parent_task_id is not None:
            task.parent_task_id = parent_task_id
        task.updated_at = _now()
        await self.db.flush()
        if status is not None and old_status != task.status:
            await self.add_event(task, "status_changed", from_status=old_status, to_status=task.status, message=error)
        elif progress is not None:
            await self.add_event(task, "progress", to_status=task.status, payload=progress)
        return task

    async def update_subject(
        self,
        subject_type: str,
        subject_id: UUID,
        *,
        status: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> TaskRun | None:
        task = await self.get_by_subject(subject_type, subject_id)
        if not task:
            return None
        return await self.update_task(task, status=status, progress=progress, result=result, error=error, meta=meta)

    async def ensure_download_task(self, job: DownloadJob, *, parent_task_id: UUID | None = None) -> TaskRun:
        progress = job.progress_data if isinstance(job.progress_data, dict) else None
        return await self.create_task(
            kind="download",
            operation_type="download",
            subject_type="download_job",
            subject_id=job.id,
            parent_task_id=parent_task_id,
            status=job.status,
            queue_name="downloads",
            title=f"Download {job.source}",
            source=job.source,
            source_url=job.source_url,
            progress=progress,
            error=job.error_log,
            meta={
                "subscription_id": str(job.subscription_id) if job.subscription_id else None,
                "subscription_source_id": str(job.subscription_source_id) if job.subscription_source_id else None,
                "retry_count": job.retry_count,
            },
        )

    async def ensure_import_task(self, job: ImportJob, *, parent_task_id: UUID | None = None) -> TaskRun:
        progress = job.progress_data if isinstance(job.progress_data, dict) else None
        if not parent_task_id:
            parent = await self.get_by_subject("download_job", job.download_job_id)
            parent_task_id = parent.id if parent else None
        return await self.create_task(
            kind="import",
            operation_type="import",
            subject_type="import_job",
            subject_id=job.id,
            parent_task_id=parent_task_id,
            status=job.status,
            queue_name="imports",
            title="Import metadata",
            progress=progress,
            error=job.error_log,
            meta={
                "download_job_id": str(job.download_job_id),
                "import_retry_count": job.import_retry_count,
                "max_import_retries": job.max_import_retries,
            },
        )

    async def list_tasks(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        operation_type: str | None = None,
        source: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[TaskRun]]:
        stmt = select(TaskRun)
        count_stmt = select(func.count(TaskRun.id))
        filters = []
        if kind and kind != "all":
            filters.append(TaskRun.kind == kind)
        if status:
            filters.append(TaskRun.status == normalize_task_status(status))
        if operation_type:
            filters.append(TaskRun.operation_type == operation_type)
        if source:
            filters.append(TaskRun.source == source)
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(or_(
                cast(TaskRun.id, String).ilike(pattern),
                cast(TaskRun.subject_id, String).ilike(pattern),
                TaskRun.title.ilike(pattern),
                TaskRun.operation_type.ilike(pattern),
                TaskRun.source_url.ilike(pattern),
                TaskRun.error_log.ilike(pattern),
            ))
        for item in filters:
            stmt = stmt.where(item)
            count_stmt = count_stmt.where(item)
        total = (await self.db.execute(count_stmt)).scalar_one()
        result = await self.db.execute(stmt.order_by(TaskRun.created_at.desc()).offset(offset).limit(limit))
        return int(total), list(result.scalars().all())

    async def task_events(self, task_id: UUID) -> list[TaskEvent]:
        result = await self.db.execute(
            select(TaskEvent).where(TaskEvent.task_run_id == task_id).order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
        )
        return list(result.scalars().all())


def task_payload(task: TaskRun, events: list[TaskEvent] | None = None) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "kind": task.kind,
        "operation_type": task.operation_type,
        "subject_type": task.subject_type,
        "subject_id": str(task.subject_id) if task.subject_id else None,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "status": task.status,
        "queue_name": task.queue_name,
        "rq_job_id": task.rq_job_id,
        "title": task.title,
        "source": task.source,
        "source_url": task.source_url,
        "progress_stage": task.progress_stage,
        "progress_current": task.progress_current,
        "progress_total": task.progress_total,
        "progress_data": task.progress_data,
        "result_data": task.result_data,
        "error_log": task.error_log,
        "meta": task.meta,
        "priority": task.priority,
        "attempts": task.attempts,
        "enqueued_at": task.enqueued_at.isoformat() if task.enqueued_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "last_heartbeat_at": task.last_heartbeat_at.isoformat() if task.last_heartbeat_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "events": [
            {
                "id": event.id,
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "message": event.message,
                "payload": event.payload,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in (events or [])
        ],
    }
