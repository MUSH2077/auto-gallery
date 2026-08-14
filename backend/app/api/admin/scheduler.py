"""Scheduler control."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.services.redis_client import get_redis

from ._routers import tasks_ops_router

class SchedulerSyncNowRequest(BaseModel):
    mode: Literal["force_eligible", "due_scan", "manual_all_enabled"] = "force_eligible"


@tasks_ops_router.post("/scheduler/sync-now")
async def trigger_sync_now(data: SchedulerSyncNowRequest | None = None, db: AsyncSession = Depends(get_db)):
    """Manually trigger subscription sync work.

    ``force_eligible`` means "sync everything the operator would reasonably
    expect to sync now": active/enabled/downloadable sources with healthy auth,
    valid URLs, and no running job.  It bypasses schedule windows only.
    ``manual_all_enabled`` additionally includes subscriptions configured with
    the explicit manual strategy while preserving every schedule setting.
    ``due_scan`` preserves the old behavior: run the scheduler's due-only scan.
    """
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.subscription_enqueue import enqueue_subscription_source_sync
    from app.services.tasks import TaskService

    mode = (data or SchedulerSyncNowRequest()).mode
    task_service = TaskService(db)
    task_title = {
        "force_eligible": "Sync eligible subscription sources",
        "manual_all_enabled": "Sync all enabled subscription sources",
        "due_scan": "Run scheduler sync scan",
    }[mode]
    parent_task = await task_service.create_task(
        kind="admin",
        operation_type="subscription-sync-batch",
        title=task_title,
        status="running",
        queue_name="scheduled" if mode == "due_scan" else "downloads",
        progress={"phase": "scanning", "label": "Preparing subscription sync", "current": 0, "total": 0},
        meta={"mode": mode},
    )
    await db.commit()

    if mode == "due_scan":
        from app.jobs.subscription_sync import sync_subscriptions_async

        summary = await sync_subscriptions_async(parent_task_id=parent_task.id)
        result = {
            "mode": mode,
            "enqueued_count": int(summary.get("created", 0) or 0),
            "skipped_count": int(summary.get("skipped", 0) or 0),
            "error_count": int(summary.get("errors", 0) or 0),
            "skipped_reasons": {summary["reason"]: 1} if summary.get("reason") else {},
            "job_ids": [],
            "rescheduled_at": summary.get("rescheduled_at"),
        }
        await task_service.update_task(
            parent_task,
            status="complete" if summary.get("status") != "partial_error" else "failed",
            result=result,
            progress={"phase": "complete", "label": "Scheduler scan complete", "current": 1, "total": 1},
            error=summary.get("reason") if summary.get("status") == "skipped" else None,
        )
        await db.commit()
        return {"status": summary.get("status", "ok"), "task_id": str(parent_task.id), "message": "Scheduler scan complete", **result}

    eligibility = [
        Subscription.is_active == True,  # noqa: E712
        SubscriptionSource.is_enabled == True,  # noqa: E712
    ]
    if mode == "force_eligible":
        eligibility.append(Subscription.sync_enabled == True)  # noqa: E712

    rows = list((await db.execute(
        select(SubscriptionSource)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .where(and_(*eligibility))
        .order_by(SubscriptionSource.last_synced_at.asc().nullsfirst(), SubscriptionSource.created_at.asc())
    )).scalars().all())

    await task_service.update_task(
        parent_task,
        progress={"phase": "enqueuing", "label": "Enqueuing eligible sources", "current": 0, "total": len(rows)},
    )
    await db.commit()

    job_ids: list[str] = []
    task_ids: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    skipped_reasons: dict[str, int] = {}

    for index, ss in enumerate(rows, start=1):
        result = await enqueue_subscription_source_sync(
            db,
            ss.id,
            trigger="manual_scheduler_batch",
            force=False,
            parent_task_id=parent_task.id,
            force_reason=(
                "scheduler_manual_all_enabled"
                if mode == "manual_all_enabled"
                else "scheduler_force_eligible"
            ),
        )
        if result.get("status") == "enqueued":
            job_ids.append(result["job_id"])
            if result.get("task_id"):
                task_ids.append(result["task_id"])
        elif result.get("status") == "error":
            errors.append(result)
            code = result.get("reason", {}).get("code") or result.get("skip_reason") or "error"
            skipped_reasons[code] = skipped_reasons.get(code, 0) + 1
        else:
            skipped.append(result)
            code = result.get("skip_reason") or result.get("reason", {}).get("code") or "skipped"
            skipped_reasons[code] = skipped_reasons.get(code, 0) + 1
        await task_service.update_task(
            parent_task,
            progress={"phase": "enqueuing", "label": "Enqueuing eligible sources", "current": index, "total": len(rows)},
        )
        await db.commit()

    response = {
        "mode": mode,
        "candidate_count": len(rows),
        "enqueued_count": len(job_ids),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "skipped_reasons": skipped_reasons,
        "job_ids": job_ids,
        "task_ids": task_ids,
        "skipped": skipped,
        "errors": errors,
    }
    status = "partial_error" if errors else "ok"
    await task_service.update_task(
        parent_task,
        status="complete" if not errors else "failed",
        result=response,
        progress={"phase": "complete", "label": "Subscription sync batch complete", "current": len(rows), "total": len(rows)},
        error=f"{len(errors)} enqueue attempts failed" if errors else None,
    )
    await db.commit()
    return {"status": status, "task_id": str(parent_task.id), "message": f"Enqueued {len(job_ids)} download jobs, skipped {len(skipped)} sources", **response}


# ── Search ──
