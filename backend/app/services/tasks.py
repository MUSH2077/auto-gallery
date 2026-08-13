"""Unified task-run service.

Download/import domain tables remain authoritative for their payloads. TaskRun
is the user-facing task envelope that lets operations and domain jobs share one
queue/progress surface.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.task_run import TaskEvent, TaskRun


NONTERMINAL_STATUSES = {"enqueued", "running", "paused", "recovering"}
TERMINAL_STATUSES = {"complete", "failed", "cancelled", "stale"}
_UNSET = object()


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
        result: dict[str, Any] | None | object = _UNSET,
        error: str | None | object = _UNSET,
        meta: dict[str, Any] | None = None,
        resource_state: str | None = None,
        resource_reason: str | None = None,
        reason_code: str | None = None,
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
                    resource_state=resource_state,
                    resource_reason=(
                        resource_reason
                        if resource_reason is not None
                        else _UNSET
                    ),
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
            resource_state=(
                resource_state
                or ("running" if normalized == "running" else "waiting")
            ),
            resource_reason=resource_reason,
            attention_state="open" if normalized in {"failed", "stale"} else "none",
            reason_code=reason_code or (
                "lost_heartbeat" if normalized == "stale"
                else "task_failed" if normalized == "failed"
                else None
            ),
            queue_name=queue_name,
            rq_job_id=rq_job_id,
            title=title,
            source=source,
            source_url=source_url,
            progress_stage=(progress or {}).get("stage") or (progress or {}).get("phase"),
            progress_current=(progress or {}).get("current"),
            progress_total=(progress or {}).get("total"),
            progress_data=progress,
            result_data=None if result is _UNSET else result,
            error_log=None if error is _UNSET else error,
            meta=meta,
            enqueued_at=now if normalized == "enqueued" else None,
            started_at=now if normalized == "running" else None,
            finished_at=now if normalized in TERMINAL_STATUSES else None,
            compactable_at=(
                now + (timedelta(hours=24) if kind == "admin" else timedelta())
                if normalized == "complete"
                else None
            ),
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
        result: dict[str, Any] | None | object = _UNSET,
        error: str | None | object = _UNSET,
        meta: dict[str, Any] | None = None,
        rq_job_id: str | None = None,
        parent_task_id: UUID | None = None,
        resource_state: str | None = None,
        resource_reason: str | None | object = _UNSET,
        attention_state: str | None = None,
        reason_code: str | None | object = _UNSET,
    ) -> TaskRun:
        old_status = task.status
        if status is not None:
            task.status = normalize_task_status(status)
            now = _now()
            if task.status == "enqueued":
                task.enqueued_at = now
                task.started_at = None
                task.finished_at = None
                # A retry is an active operation, no longer an unresolved
                # anomaly.  Keep reason_code so a successful retry can enter
                # the seven-day resolved window.
                if task.attention_state == "open":
                    task.attention_state = "none"
                if resource_state is None:
                    task.resource_state = "waiting"
            elif task.status == "running":
                if not task.started_at:
                    task.started_at = now
                task.finished_at = None
                if task.attention_state == "open":
                    task.attention_state = "none"
            elif task.status in {"paused", "recovering"}:
                task.finished_at = None
                if resource_state is None:
                    task.resource_state = "yielded" if task.status == "paused" else "waiting"
            elif task.status in TERMINAL_STATUSES:
                task.finished_at = now
                if resource_state is None:
                    task.resource_state = "yielded"
                    task.resource_reason = None
                if task.status in {"failed", "stale"}:
                    task.attention_state = "open"
                    if reason_code is _UNSET and not task.reason_code:
                        task.reason_code = (
                            "lost_heartbeat" if task.status == "stale" else "task_failed"
                        )
                    task.resolved_at = None
                    task.compactable_at = None
                elif task.status == "complete":
                    if task.reason_code:
                        task.attention_state = "resolved"
                        task.resolved_at = now
                        task.compactable_at = now + timedelta(days=7)
                    else:
                        task.attention_state = "none"
                        task.compactable_at = now + (
                            timedelta(hours=24) if task.kind == "admin" else timedelta()
                        )
                elif task.status == "cancelled":
                    # Cancellation is an expected user terminal state, not an
                    # anomaly. Keep its technical detail for one day only.
                    task.attention_state = "none"
                    task.reason_code = None
                    task.resolved_at = None
                    task.compactable_at = now + timedelta(hours=24)
        if progress is not None:
            task.progress_data = progress
            task.progress_stage = progress.get("stage") or progress.get("phase")
            task.progress_current = progress.get("current") or progress.get("scanned")
            task.progress_total = progress.get("total")
        if result is not _UNSET:
            task.result_data = result
        if error is not _UNSET:
            task.error_log = error
        if meta is not None:
            task.meta = meta
        if rq_job_id is not None:
            task.rq_job_id = rq_job_id
        if parent_task_id is not None:
            task.parent_task_id = parent_task_id
        if resource_state is not None:
            if resource_state not in {"running", "waiting", "yielded"}:
                raise ValueError(f"invalid resource_state: {resource_state}")
            task.resource_state = resource_state
        if resource_reason is not _UNSET:
            task.resource_reason = resource_reason
        if attention_state is not None:
            if attention_state not in {"none", "open", "resolved", "acknowledged"}:
                raise ValueError(f"invalid attention_state: {attention_state}")
            task.attention_state = attention_state
            if attention_state == "resolved" and not task.resolved_at:
                task.resolved_at = _now()
                task.compactable_at = task.resolved_at + timedelta(days=7)
            elif attention_state == "acknowledged":
                task.acknowledged_at = _now()
                task.resolved_at = task.resolved_at or task.acknowledged_at
                task.compactable_at = task.resolved_at + timedelta(days=7)
        if reason_code is not _UNSET:
            task.reason_code = reason_code
        task.updated_at = _now()
        await self.db.flush()
        if status is not None and old_status != task.status:
            await self.add_event(
                task,
                "status_changed",
                from_status=old_status,
                to_status=task.status,
                message=None if error is _UNSET else error,
            )
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
        result: dict[str, Any] | None | object = _UNSET,
        error: str | None | object = _UNSET,
        meta: dict[str, Any] | None = None,
        resource_state: str | None = None,
        resource_reason: str | None | object = _UNSET,
        attention_state: str | None = None,
        reason_code: str | None | object = _UNSET,
    ) -> TaskRun | None:
        task = await self.get_by_subject(subject_type, subject_id)
        if not task:
            return None
        return await self.update_task(
            task,
            status=status,
            progress=progress,
            result=result,
            error=error,
            meta=meta,
            resource_state=resource_state,
            resource_reason=resource_reason,
            attention_state=attention_state,
            reason_code=reason_code,
        )

    async def ensure_download_task(self, job: DownloadJob, *, parent_task_id: UUID | None = None) -> TaskRun:
        from app.services.sync_outcome import download_job_outcome

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
            result=download_job_outcome(job),
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
        include_account: bool = False,
        visibility: str = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[TaskRun]]:
        stmt = select(TaskRun)
        count_stmt = select(func.count(TaskRun.id))
        if visibility == "actionable":
            actionable = or_(
                TaskRun.status.in_({"enqueued", "running", "recovering", "paused"}),
                TaskRun.attention_state == "open",
            )
            stmt = stmt.where(actionable)
            count_stmt = count_stmt.where(actionable)
        filters = []
        if kind and kind != "all":
            filters.append(TaskRun.kind == kind)
        elif not kind and not include_account:
            filters.append(TaskRun.kind != "account")
        if status:
            filters.append(TaskRun.status == normalize_task_status(status))
        if operation_type:
            filters.append(TaskRun.operation_type == operation_type)
        if source:
            filters.append(TaskRun.source == source)
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
        "resource_state": task.resource_state,
        "resource_reason": task.resource_reason,
        "attention_state": task.attention_state,
        "reason_code": task.reason_code,
        "acknowledged_at": task.acknowledged_at.isoformat() if task.acknowledged_at else None,
        "resolved_at": task.resolved_at.isoformat() if task.resolved_at else None,
        "compactable_at": task.compactable_at.isoformat() if task.compactable_at else None,
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


async def update_task_resource_state(
    owner: str,
    state: str,
    reason: str | None,
) -> None:
    """Bridge a resource-profile owner to its user-facing TaskRun.

    Download/import jobs use their domain UUID as ``owner``; admin operations
    use the TaskRun UUID directly.  One indexed OR lookup handles both and a
    no-change transition performs no write/event, keeping idle control-plane
    write volume low.
    """
    try:
        owner_id = UUID(str(owner))
    except (TypeError, ValueError):
        return
    from app.database import async_session

    async with async_session() as db:
        task = (
            await db.execute(
                select(TaskRun)
                .where(
                    (TaskRun.id == owner_id) | (TaskRun.subject_id == owner_id)
                )
                .order_by((TaskRun.id == owner_id).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if task is None or (
            task.resource_state == state and task.resource_reason == reason
        ):
            return
        task.resource_state = state
        task.resource_reason = reason
        task.updated_at = _now()
        await db.commit()
