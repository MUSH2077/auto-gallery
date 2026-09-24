"""Bounded durable candidate selection for admin dispatch recovery."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task_run import TaskRun

ADMIN_DISPATCH_META_KEY = "admin_dispatch"
ADMIN_DISPATCH_PENDING = "pending"
ADMIN_DISPATCH_PUBLISHED = "published"
ADMIN_DISPATCH_FAILED = "failed"
ADMIN_DISPATCH_RECOVERY_LIMIT = 25
ADMIN_DISPATCH_GRACE_SECONDS = 15
_ACTIVE_ADMIN_STATUSES = frozenset({"enqueued", "running", "paused", "recovering"})


def _admin_dispatch(task) -> dict[str, Any] | None:
    value = (task.meta or {}).get(ADMIN_DISPATCH_META_KEY)
    return dict(value) if isinstance(value, dict) else None


async def select_due_admin_dispatches(
    db: AsyncSession,
    *,
    now: datetime,
    grace_seconds: int,
    limit: int,
    include_published: bool,
) -> list[tuple[UUID, int]]:
    """Select a bounded, deterministic set of due durable dispatches."""

    bounded_limit = max(1, min(int(limit), ADMIN_DISPATCH_RECOVERY_LIMIT))
    prepared_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["prepared_at"].astext
    prepared_at = cast(prepared_text, DateTime(timezone=True))
    retry_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["next_retry_at"].astext
    retry_at = cast(retry_text, DateTime(timezone=True))
    grace_cutoff = now - timedelta(seconds=max(0, grace_seconds))
    common_filters = (
        TaskRun.kind == "admin",
        TaskRun.status.in_(_ACTIVE_ADMIN_STATUSES),
        prepared_at <= grace_cutoff,
    )

    pending = list(
        (
            await db.execute(
                select(TaskRun)
                .where(
                    *common_filters,
                    TaskRun.meta[ADMIN_DISPATCH_META_KEY]["publication_state"]
                    .astext
                    == ADMIN_DISPATCH_PENDING,
                    or_(retry_text.is_(None), retry_at <= now),
                )
                .order_by(
                    retry_at.asc().nullsfirst(),
                    prepared_at.asc(),
                    TaskRun.id.asc(),
                )
                .limit(bounded_limit)
            )
        ).scalars()
    )
    due = [
        (task.id, int((_admin_dispatch(task) or {}).get("attempt") or 0))
        for task in pending
    ]

    remaining = bounded_limit - len(due)
    if include_published and remaining > 0:
        probe_text = TaskRun.meta[ADMIN_DISPATCH_META_KEY]["next_probe_at"].astext
        probe_at = cast(probe_text, DateTime(timezone=True))
        published = list(
            (
                await db.execute(
                    select(TaskRun)
                    .where(
                        *common_filters,
                        TaskRun.meta[ADMIN_DISPATCH_META_KEY][
                            "publication_state"
                        ].astext
                        == ADMIN_DISPATCH_PUBLISHED,
                        or_(probe_text.is_(None), probe_at <= now),
                    )
                    .order_by(
                        probe_at.asc().nullsfirst(),
                        prepared_at.asc(),
                        TaskRun.id.asc(),
                    )
                    .limit(remaining)
                )
            ).scalars()
        )
        due.extend(
            (
                task.id,
                int((_admin_dispatch(task) or {}).get("attempt") or 0),
            )
            for task in published
        )
    return due
