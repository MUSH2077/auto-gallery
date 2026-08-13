"""Idempotently reconcile, receipt, compact, and repair operational history."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

from app.database import async_session
from app.models import DownloadJob, TaskEvent, TaskRun
from app.services.admin_data import clear_failed_rq_jobs
from app.services.operation_attention import (
    compact_terminal_tasks,
    reconcile_task_truth,
    upsert_repository_sync_receipt,
)


async def _backfill_receipts(*, apply: bool, batch_size: int = 500) -> dict[str, int]:
    scanned = written = 0
    cursor = None
    while True:
        async with async_session() as db:
            statement = (
                select(DownloadJob)
                .where(
                    DownloadJob.subscription_source_id.is_not(None),
                    DownloadJob.status.in_({"complete", "failed", "stale", "cancelled"}),
                )
                .order_by(DownloadJob.id)
                .limit(batch_size)
            )
            if cursor is not None:
                statement = statement.where(DownloadJob.id > cursor)
            jobs = list((await db.execute(statement)).scalars())
            if not jobs:
                break
            scanned += len(jobs)
            if apply:
                for job in jobs:
                    if await upsert_repository_sync_receipt(db, job):
                        written += 1
                await db.commit()
            cursor = jobs[-1].id
    return {"scanned": scanned, "written": written}


async def _retry_reconciled_downloads(report: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.services.task_engine import TaskEngine
    from app.services.tasks import TaskService

    retried: list[str] = []
    failed: list[dict[str, str]] = []
    task_ids = {
        issue["task_id"]
        for issue in report.get("issues", [])
        if issue.get("subject_type") == "download_job"
        and issue.get("reason_code") != "orphaned_subject"
        and issue.get("to_status") in {"failed", "stale"}
    }
    # The backend's periodic reconciler may have repaired the five false
    # running rows while deployment health checks were still completing. Pick
    # up those durable reconciliation events as well, but only once.
    async with async_session() as db:
        previously_reconciled = (
            await db.execute(
                select(TaskRun.id)
                .where(
                    TaskRun.subject_type == "download_job",
                    TaskRun.status.in_({"failed", "stale"}),
                    TaskRun.reason_code.in_({"child_import_failed", "lost_heartbeat"}),
                    or_(
                        TaskRun.meta.is_(None),
                        TaskRun.meta["reconciliation_retry_at"].astext.is_(None),
                    ),
                    select(TaskEvent.id)
                    .where(
                        TaskEvent.task_run_id == TaskRun.id,
                        TaskEvent.event_type == "reconciled",
                    )
                    .exists(),
                )
            )
        ).scalars()
        task_ids.update(str(task_id) for task_id in previously_reconciled)
    for task_id in task_ids:
        async with async_session() as db:
            task = await db.get(TaskRun, UUID(task_id))
            if (
                not task
                or not task.subject_id
                or task.status not in {"failed", "stale"}
                or (task.meta or {}).get("reconciliation_retry_at")
            ):
                continue
            try:
                await TaskEngine(db).retry_download(
                    task.subject_id,
                    operator="operation-history-reconcile",
                )
                task.meta = {
                    **(task.meta or {}),
                    "reconciliation_retry_at": datetime.now(timezone.utc).isoformat(),
                }
                await TaskService(db).add_event(
                    task,
                    "reconciliation_retry",
                    to_status=task.status,
                    message="One-time retry after task truth reconciliation",
                )
                await db.commit()
                retried.append(str(task.subject_id))
            except Exception as exc:  # keep repairing other independent tasks
                await db.rollback()
                failed.append({"task_id": task_id, "error": str(exc)})
    return {"retried": retried, "failed": failed}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    async with async_session() as db:
        reconciliation = await reconcile_task_truth(
            db,
            dry_run=not args.apply,
            limit=args.reconcile_limit,
        )
    receipts = await _backfill_receipts(apply=args.apply)
    retry_result = {"retried": [], "failed": []}
    if args.apply and args.retry_reconciled_downloads:
        retry_result = await _retry_reconciled_downloads(reconciliation)

    compaction_batches: list[dict[str, Any]] = []
    if args.compact:
        while True:
            async with async_session() as db:
                batch = await compact_terminal_tasks(
                    db,
                    dry_run=not args.apply,
                    limit=args.compact_batch_size,
                )
            compaction_batches.append(batch)
            if not args.apply or not batch.get("deleted_tasks"):
                break

    rq_removed = 0
    if args.clear_orphaned_rq:
        async with async_session() as db:
            rq_removed = await clear_failed_rq_jobs(db, dry_run=not args.apply)

    return {
        "apply": args.apply,
        "reconciliation": reconciliation,
        "receipts": receipts,
        "retry": retry_result,
        "compaction_batches": compaction_batches,
        "rq_failed_entries": rq_removed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--retry-reconciled-downloads", action="store_true")
    parser.add_argument("--clear-orphaned-rq", action="store_true")
    parser.add_argument("--reconcile-limit", type=int, default=2000)
    parser.add_argument("--compact-batch-size", type=int, default=200)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
