"""Data management: clear entities, library rebuild, disk import, reindex, re-enrich, schedule backup."""

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
from app.services.operations import get_operation_status, set_operation_status
from app.services import admin_data
from app.services.admin_data import ENTITIES, clear_entity_data

from ._routers import router, _clear_files

def _job_is_sync_scan(job) -> bool:
    return "sync_subscriptions" in (getattr(job, "func_name", "") or str(job))


def _reschedule_subscription_sync_scan(config: dict) -> dict:
    """Replace pending subscription sync scan jobs with one using current config."""
    import redis as redis_lib
    from rq import Queue
    from rq.registry import ScheduledJobRegistry
    from app.jobs.subscription_sync import sync_subscriptions

    interval = max(int(config.get("scheduler_scan_interval_minutes", 60)), 5)
    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="scheduled", connection=r)
    scheduled_registry = ScheduledJobRegistry(queue=q)

    removed = 0
    for job_id in list(scheduled_registry.get_job_ids()):
        job = q.fetch_job(job_id)
        if job and _job_is_sync_scan(job):
            scheduled_registry.remove(job_id, delete_job=True)
            removed += 1

    for job in list(q.get_jobs()):
        if _job_is_sync_scan(job):
            q.remove(job.id)
            removed += 1

    job = q.enqueue_in(timedelta(minutes=interval), sync_subscriptions)
    return {"removed": removed, "job_id": job.id, "interval_minutes": interval}


DEFAULT_DEDUP = {
    "auto_group_enabled": True,
    "phash_threshold": 4,
    "ssim_threshold": 0.98,
    "aspect_ratio_tolerance": 0.01,
    "auto_group_score": 95,
    "review_score": 70,
    "quarantine_days": 30,
}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False}


# ── Settings ──




@router.post("/cleanup-metadata-jsons")
async def cleanup_metadata_jsons():
    """Remove all gallery-dl metadata JSON files from downloads directory."""
    from app.jobs.import_runner import cleanup_metadata_jsons
    removed = await cleanup_metadata_jsons(settings.download_root)
    return {"status": "ok", "removed": removed}


@router.get("/import-progress")
async def import_progress():
    """Get current import job progress summary."""
    from app.models.import_job import ImportJob as IJ
    async with async_session() as db:
        result = await db.execute(
            select(IJ).order_by(IJ.created_at.desc()).limit(20)
        )
        jobs = result.scalars().all()
        return {
            "running": sum(1 for j in jobs if j.status == "running"),
            "enqueued": sum(1 for j in jobs if j.status == "enqueued"),
            "complete": sum(1 for j in jobs if j.status == "complete"),
            "failed": sum(1 for j in jobs if j.status == "failed"),
            "recent": [{"id": str(j.id), "status": j.status, "error": (j.error_log or "")[:200]} for j in jobs[:5]],
        }



@router.post("/backup/schedule")
async def schedule_backup(data: dict):
    """Schedule recurring auto-backup. {enabled: bool, interval_hours: int}"""
    import redis as redis_lib
    from rq import Queue
    from datetime import timedelta

    enabled = str(data.get("enabled", "")).lower() in ("true", "1", "yes")
    interval = int(data.get("interval_hours", 24))
    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="scheduled", connection=r)

    # Remove existing backup jobs
    for jid in list(q.scheduled_job_registry.get_job_ids()):
        job = q.fetch_job(jid)
        if job and "auto_backup" in str(job.func_name):
            q.remove(jid)

    if enabled:
        q.enqueue_in(timedelta(hours=interval),
            "app.jobs.backup.run_auto_backup", interval=interval, job_timeout=3600)
        return {"status": "ok", "message": f"Auto-backup enabled, every {interval}h"}
    return {"status": "ok", "message": "Auto-backup disabled"}


_system_info_cache: dict | None = None
_system_info_cache_ts: float = 0.0
_SYSTEM_INFO_CACHE_TTL = 60.0
_system_info_lock = asyncio.Lock()

@router.post("/clear/{entity}")
async def clear_entity(entity: str, db: AsyncSession = Depends(get_db)):
    """Clear a specific entity or 'all' for complete cleanup."""
    try:
        admin_data._clear_files = _clear_files
        return await clear_entity_data(entity, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}. Valid: all, {', '.join(ENTITIES.keys())}",
        ) from exc


@router.post("/operations/clear")
async def start_clear_operation(data: dict, db: AsyncSession = Depends(get_db)):
    """Enqueue a data-management clear operation and return immediately."""
    from rq import Queue

    entity = str(data.get("entity") or "").strip()
    if entity != "all" and entity not in ENTITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity: {entity}. Valid: all, {', '.join(ENTITIES.keys())}",
        )

    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    task = await TaskService(db).create_task(
        task_id=UUID(job_id),
        kind="admin",
        operation_type="admin-clear",
        title=f"Clear {entity}",
        status="enqueued",
        queue_name="operations",
        meta={"entity": entity},
        progress={"phase": "enqueued", "label": f"Queued clear for {entity}"},
    )
    await db.commit()
    set_operation_status(
        job_id,
        "enqueued",
        "admin-clear",
        progress={"phase": "enqueued", "label": f"Queued clear for {entity}"},
        meta={"entity": entity},
    )
    queue = Queue(name="operations", connection=get_redis())
    rq_job = queue.enqueue(
        "app.jobs.admin_operations.run_clear_operation",
        entity,
        job_id,
        job_timeout=7200,
        result_ttl=7200,
    )
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()
    logger.info("Enqueued admin clear operation job_id=%s rq_job=%s entity=%s", job_id, rq_job.id, entity)
    return {"job_id": job_id, "status": "enqueued"}


@router.get("/operations/{job_id}")
async def get_admin_operation(job_id: str):
    status = get_operation_status(job_id)
    if not status:
        try:
            from app.services.tasks import TaskService, task_payload
            async with async_session() as db:
                task = await TaskService(db).get(UUID(job_id))
                if task and task.kind == "admin":
                    payload = task_payload(task)
                    return {
                        "job_id": payload["id"],
                        "status": payload["status"],
                        "operation_type": payload["operation_type"],
                        "progress": payload["progress_data"],
                        "result": payload["result_data"],
                        "error": payload["error_log"],
                        "meta": payload["meta"],
                        "updated_at": task.updated_at.timestamp() if task.updated_at else None,
                    }
        except ValueError:
            pass
        raise HTTPException(status_code=404, detail="Operation not found")
    return status


@router.get("/operations")
async def list_active_operations():
    """List all active (running + enqueued) admin operations."""
    import json
    from app.services.redis_client import get_redis
    r = get_redis()
    keys = r.keys("admin_operation:*")
    ops = []
    for k in keys:
        raw = r.get(k)
        if raw:
            try:
                payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                if payload.get("status") in ("queued", "enqueued", "running"):
                    ops.append(payload)
            except Exception:
                pass
    ops.sort(key=lambda o: o.get("updated_at", 0), reverse=True)
    return {"operations": ops}

@router.post("/search/reindex")
async def reindex_search():
    """Enqueue a full Meilisearch reindex — a whole-library walk that belongs
    in a worker, not inline in the backend process."""
    from app.services.operations import enqueue_admin_operation
    return await enqueue_admin_operation(
        lock_key="library:search-reindex:active",
        operation_type="admin-search-reindex",
        title="Search reindex",
        entity="search-reindex",
        func="app.jobs.admin_operations.run_search_reindex_operation",
    )


# ── Library ──

class RebuildLibraryRequest(BaseModel):
    mode: Literal["repair", "full"] = "repair"
    source: str | None = None
    creator_id: UUID | None = None
    work_id: UUID | None = None
    resume: bool = True


@router.post("/library/rebuild")
async def rebuild_library(data: RebuildLibraryRequest | None = None):
    """Enqueue a library rebuild operation and return immediately."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status

    options = (data or RebuildLibraryRequest()).model_dump(mode="json")
    redis = get_redis()
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    active_job = redis.get("library:rebuild:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:rebuild:active")
    if not redis.set("library:rebuild:active", job_id, nx=True, ex=604800):
        active_job = redis.get("library:rebuild:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Library rebuild already running", "job_id": active_job})
    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-rebuild",
            title="Rebuild library index",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Library rebuild queued"},
            meta={"entity": "library", **options},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-rebuild",
        progress={"phase": "enqueued", "label": "Library rebuild queued"},
        meta={"entity": "library", **options})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_library_rebuild_operation",
        job_id, options, job_timeout=14400, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Library rebuild queued", "options": options}


class ImportFromDiskRequest(BaseModel):
    source: str | None = None
    # Reprocess every on-disk file even if the ledger marks it 'done'. Use this
    # to recover creators/works after they were deleted from the DB (the ledger
    # is not reset by deletion, so a normal disk-import skips them as 'done').
    reset_ledger: bool = False


@router.post("/library/import-from-disk")
async def import_from_disk(data: ImportFromDiskRequest | None = None):
    """Enqueue an idempotent import of on-disk download files into the DB."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status

    options = (data or ImportFromDiskRequest()).model_dump(mode="json")
    redis = get_redis()
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    active_job = redis.get("library:disk-import:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:disk-import:active")
    if not redis.set("library:disk-import:active", job_id, nx=True, ex=604800):
        active_job = redis.get("library:disk-import:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Disk import already running", "job_id": active_job})
    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-disk-import",
            title="Import from disk",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Disk import queued"},
            meta={"entity": "disk-import", **options},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-disk-import",
        progress={"phase": "enqueued", "label": "Disk import queued"},
        meta={"entity": "disk-import", **options})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_disk_import_operation",
        job_id, options, job_timeout=14400, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Disk import queued", "options": options}


@router.post("/creators/re-enrich")
async def reenrich_creators():
    """Enqueue a Danbooru re-enrichment sweep for creators flagged needs_enrichment."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status
    from app.services.tasks import TaskService

    redis = get_redis()
    job_id = str(uuid.uuid4())
    active_job = redis.get("library:creator-reenrich:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:creator-reenrich:active")
    if not redis.set("library:creator-reenrich:active", job_id, nx=True, ex=86400):
        active_job = redis.get("library:creator-reenrich:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={
            "message": "Creator re-enrichment already running", "job_id": active_job})
    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-creator-reenrich",
            title="Re-enrich creators from Danbooru",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Creator re-enrichment queued"},
            meta={"entity": "creator-reenrich"},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-creator-reenrich",
        progress={"phase": "enqueued", "label": "Creator re-enrichment queued"},
        meta={"entity": "creator-reenrich"})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_creator_reenrich_operation",
        job_id, {}, job_timeout=7200, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Creator re-enrichment queued"}


# ── gallery-dl Config ──
