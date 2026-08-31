"""RQ execution boundary for persistent remote-follow discovery scans."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import update

from app.database import async_session
from app.models import TaskRun
from app.services.remote_discovery import RemoteDiscoveryService


async def _run(task_id: UUID):
    async with async_session() as db:
        return await RemoteDiscoveryService(db).run_scan(task_id)


async def _prepare_rq_retry(task_id: UUID) -> None:
    async with async_session() as db:
        await db.execute(
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.operation_type == "remote-discovery-scan",
                TaskRun.status == "failed",
            )
            .values(
                status="recovering",
                resource_state="waiting",
                finished_at=None,
                last_heartbeat_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


def run_remote_discovery_scan(task_id: str):
    """Run one opaque persistent task and preserve its state for RQ retries."""

    task_uuid = UUID(task_id)
    try:
        return asyncio.run(_run(task_uuid))
    except Exception:
        retries_left = 0
        try:
            from rq import get_current_job

            current = get_current_job()
            retries_left = int(getattr(current, "retries_left", 0) or 0)
        except Exception:
            retries_left = 0
        if retries_left > 0:
            asyncio.run(_prepare_rq_retry(task_uuid))
        raise
