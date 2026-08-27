"""PostgreSQL-backed recurring backup dispatch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.database import async_session
from app.models.system_setting import SystemSetting
from app.services.operations import prepare_admin_operation, publish_admin_operation


BACKUP_SCHEDULE_KEY = "backup_schedule"
BACKUP_CONTENTS = [
    "database",
    "gallerydl-config",
    "app-config",
    "download-archives",
    "library-metadata",
]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_run(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


async def dispatch_due_backup(
    *,
    now: datetime | None = None,
    redis_client=None,
) -> dict[str, Any]:
    """Claim one due occurrence in PostgreSQL, then best-effort publish it."""

    checked_at = _utc(now or datetime.now(timezone.utc))
    async with async_session() as db:
        row = (
            await db.execute(
                select(SystemSetting)
                .where(SystemSetting.key == BACKUP_SCHEDULE_KEY)
                .with_for_update(of=SystemSetting)
            )
        ).scalar_one_or_none()
        value = dict(row.value or {}) if row is not None else {}
        if row is None or not bool(value.get("enabled")):
            await db.rollback()
            return {"created": False, "reason": "disabled"}

        interval = max(1, min(8760, int(value.get("interval_hours") or 24)))
        due_at = _next_run(value.get("next_run_at"))
        if due_at is not None and due_at > checked_at:
            await db.rollback()
            return {
                "created": False,
                "reason": "not_due",
                "next_run_at": due_at.isoformat(),
            }

        try:
            prepared = await prepare_admin_operation(
                db,
                operation_type="admin-backup-create",
                scope_key="backup:create:active",
                title="Create scheduled backup",
                entity="backup",
                options={"contents": BACKUP_CONTENTS, "trigger": "schedule"},
                queue_name="maintenance",
                job_timeout=3600,
            )
        except HTTPException as exc:
            await db.rollback()
            if exc.status_code != 409:
                raise
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return {
                "created": False,
                "reason": "active_backup",
                "task_id": detail.get("task_id"),
            }

        next_at = checked_at + timedelta(hours=interval)
        row.value = {
            **value,
            "enabled": True,
            "interval_hours": interval,
            "next_run_at": next_at.isoformat(),
            "last_task_id": str(prepared.task.id),
            "last_dispatched_at": checked_at.isoformat(),
        }
        task_id = prepared.task.id
        attempt = prepared.attempt
        await db.commit()

    publication = await publish_admin_operation(
        task_id,
        attempt,
        redis_client=redis_client,
    )
    return {
        "created": True,
        "task_id": str(task_id),
        "publication": publication,
        "next_run_at": next_at.isoformat(),
    }
