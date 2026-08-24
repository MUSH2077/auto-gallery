"""Re-evaluate retained historical staging conflicts under the current policy."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select

from app.database import async_session
from app.models import TaskRun
from app.services.download_conflicts import DownloadConflictService
from app.services.locks import redis_lock
from app.services.settings import get_download_defaults
from app.services.task_engine import TaskEngine


async def reconcile_historical_download_conflicts_unlocked(limit: int = 200) -> dict:
    stats = {"examined": 0, "resolved": 0, "requeued": 0, "manual_required": 0, "errors": 0}
    async with async_session() as db:
        defaults = await get_download_defaults(db)
        if not defaults.get("auto_resolve_upstream_conflicts", True):
            return {**stats, "status": "disabled"}
        task_ids = list((await db.execute(
            select(TaskRun.id)
            .where(
                TaskRun.subject_type == "download_job",
                TaskRun.reason_code == "download_staging_conflict",
                TaskRun.status.in_({"failed", "stale"}),
            )
            .order_by(TaskRun.updated_at, TaskRun.id)
            .limit(max(1, min(int(limit), 1000)))
        )).scalars())
    for task_id in task_ids:
        stats["examined"] += 1
        try:
            async with async_session() as db:
                service = DownloadConflictService(db)
                case = await service.inspect(task_id)
                if not case.get("all_auto_eligible"):
                    stats["manual_required"] += 1
                    continue
                decisions = {
                    item["relative_path"]: item["evidence"]["recommended_winner"]
                    for item in case["items"]
                }
                result = await service.resolve(
                    task_id,
                    decisions,
                    operator="automatic-upstream-correction",
                    automatic=True,
                )
                await db.commit()
                stats["resolved"] += 1
                try:
                    await TaskEngine(db).retry_download(
                        UUID(str(result["download_job_id"])),
                        operator="automatic-upstream-correction",
                    )
                    await db.commit()
                    stats["requeued"] += 1
                except Exception:
                    await db.rollback()
                    stats["errors"] += 1
        except Exception:
            stats["errors"] += 1
    return {**stats, "status": "complete"}


async def reconcile_historical_download_conflicts_async(limit: int = 200) -> dict:
    stats = {"examined": 0, "resolved": 0, "requeued": 0, "manual_required": 0, "errors": 0}
    async with redis_lock("lock:download-conflict-reconciliation", ttl_seconds=3600) as acquired:
        if not acquired:
            return {**stats, "status": "already_running"}
        return await reconcile_historical_download_conflicts_unlocked(limit)


def reconcile_historical_download_conflicts(limit: int = 200) -> dict:
    return asyncio.run(reconcile_historical_download_conflicts_async(limit))
