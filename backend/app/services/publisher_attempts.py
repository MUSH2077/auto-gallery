"""Durable immutable-attempt authority for bounded disk publishers."""

from __future__ import annotations

import secrets
from uuid import UUID

from sqlalchemy import select

from app.models.task_run import TaskRun


PUBLISHER_ATTEMPT_META_KEY = "_bounded_import_publisher_attempt"


def new_publisher_attempt() -> str:
    """Return a cryptographically unpredictable internal attempt token."""

    return secrets.token_urlsafe(32)


def redact_publisher_attempt(
    value: BaseException | str,
    attempt_token: str | None,
) -> str:
    """Remove private authority from text that may enter public projections."""

    text = str(value)
    if attempt_token:
        text = text.replace(attempt_token, "[redacted publisher attempt]")
    return text


def publisher_job_description(task_id: UUID | str) -> str:
    """Return the stable, public-safe RQ description for a disk publisher."""

    return f"admin-disk-import task={task_id}"


def current_publisher_attempt(task: TaskRun) -> str | None:
    meta = task.meta or {}
    # New disk-import deliveries use the registry attempt persisted on this
    # same TaskRun. Ignore any rolling-upgrade private token left by an older
    # attempt so a retry cannot fence its own successor.
    dispatch = meta.get("admin_dispatch")
    if isinstance(dispatch, dict) and task.operation_type == "admin-disk-import":
        try:
            attempt = int(dispatch.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt > 0:
            return str(attempt)
    raw = meta.get(PUBLISHER_ATTEMPT_META_KEY)
    return raw if isinstance(raw, str) and raw else None


def set_publisher_attempt(task: TaskRun, attempt_token: str) -> str:
    if not isinstance(attempt_token, str) or not attempt_token:
        raise ValueError("publisher attempt token must be a non-empty string")
    task.meta = {
        **dict(task.meta or {}),
        PUBLISHER_ATTEMPT_META_KEY: attempt_token,
    }
    return attempt_token


def ensure_publisher_attempt(
    task: TaskRun,
    *,
    captured_attempt: str | None = None,
    rotate: bool = False,
) -> tuple[str, bool]:
    """Mint/adopt one attempt while the caller holds the TaskRun row lock."""

    current = current_publisher_attempt(task)
    if rotate:
        attempt = new_publisher_attempt()
        set_publisher_attempt(task, attempt)
        return attempt, True
    if current is not None:
        return current, False
    attempt = captured_attempt or new_publisher_attempt()
    set_publisher_attempt(task, attempt)
    return attempt, True


async def lock_publisher_task(db, task_id: UUID) -> TaskRun | None:
    """Lock and refresh one publisher TaskRun in the global lifecycle order."""

    with db.no_autoflush:
        return (
            await db.execute(
                select(TaskRun)
                .where(
                    TaskRun.id == task_id,
                    TaskRun.kind == "admin",
                    TaskRun.operation_type == "admin-disk-import",
                )
                .with_for_update(of=TaskRun)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()


def public_task_meta(meta: dict | None) -> dict | None:
    """Return API-safe task metadata without internal publisher authority."""

    if meta is None:
        return None
    public = dict(meta)
    public.pop(PUBLISHER_ATTEMPT_META_KEY, None)
    return public
