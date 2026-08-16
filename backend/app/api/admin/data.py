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
from app.services.queue_admission import (
    checked_enqueue,
    checked_enqueue_in,
    ensure_redis_enqueue_capacity,
)
from app.services.operations import (
    compensate_operation_enqueue_failure,
    get_operation_status,
    release_owned_operation_lock,
    set_operation_status,
)
from app.services import admin_data
from app.services.admin_data import (
    CONFIRMATION_PHRASES,
    ENTITIES,
    clear_entity_data,
    preview_clear_entity_data,
)

from ._routers import router, _clear_files


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
    from rq import Queue
    from datetime import timedelta

    enabled = str(data.get("enabled", "")).lower() in ("true", "1", "yes")
    interval = int(data.get("interval_hours", 24))
    r = get_redis()
    q = Queue(name="scheduled", connection=r)

    # Snapshot the old jobs, publish the replacement, and only then remove the
    # old schedule. A late Redis admission failure leaves the known-good job.
    old_job_ids = []
    for jid in list(q.scheduled_job_registry.get_job_ids()):
        job = q.fetch_job(jid)
        if job and "auto_backup" in str(job.func_name):
            old_job_ids.append(jid)

    if enabled:
        new_job = checked_enqueue_in(
            q,
            timedelta(hours=interval),
            "app.jobs.backup.run_auto_backup",
            interval=interval,
            job_timeout=3600,
        )
        for jid in old_job_ids:
            if jid != new_job.id:
                q.scheduled_job_registry.remove(jid, delete_job=True)
        return {"status": "ok", "message": f"Auto-backup enabled, every {interval}h"}

    for jid in old_job_ids:
        q.scheduled_job_registry.remove(jid, delete_job=True)
    return {"status": "ok", "message": "Auto-backup disabled"}


_system_info_cache: dict | None = None
_system_info_cache_ts: float = 0.0
_SYSTEM_INFO_CACHE_TTL = 60.0
_system_info_lock = asyncio.Lock()

ClearEntity = Literal["works", "creators", "subscriptions", "tags", "jobs", "settings", "all"]


class ClearOperationRequest(BaseModel):
    entity: ClearEntity
    confirmation: str


def _validate_clear_confirmation(data: ClearOperationRequest) -> None:
    expected = CONFIRMATION_PHRASES[data.entity]
    if data.confirmation != expected:
        raise HTTPException(
            status_code=422,
            detail={"message": "Confirmation phrase does not match", "expected": expected},
        )


@router.get("/clear/preview/{entity}")
async def preview_clear_entity(entity: ClearEntity, db: AsyncSession = Depends(get_db)):
    return await preview_clear_entity_data(entity, db)

@router.post("/clear/{entity}")
async def clear_entity(entity: ClearEntity, data: ClearOperationRequest, db: AsyncSession = Depends(get_db)):
    """Clear a specific entity or 'all' for complete cleanup."""
    if data.entity != entity:
        raise HTTPException(status_code=422, detail="Path and request entity do not match")
    _validate_clear_confirmation(data)
    try:
        admin_data._clear_files = _clear_files
        return await clear_entity_data(entity, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}. Valid: all, {', '.join(ENTITIES.keys())}",
        ) from exc


@router.post("/operations/clear")
async def start_clear_operation(data: ClearOperationRequest, db: AsyncSession = Depends(get_db)):
    """Enqueue a data-management clear operation and return immediately."""
    from rq import Queue

    _validate_clear_confirmation(data)
    entity = data.entity

    redis = get_redis()
    # Reject the request before committing its TaskRun/operation projection.
    # checked_enqueue repeats the guard at the actual publication boundary.
    ensure_redis_enqueue_capacity(redis)
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    task = await TaskService(db).create_task(
        task_id=UUID(job_id),
        kind="admin",
        operation_type="admin-clear",
        title=f"Clear {entity}",
        status="enqueued",
        queue_name="maintenance",
        meta={"entity": entity},
        progress={"phase": "enqueued", "label": f"Queued clear for {entity}"},
    )
    await db.commit()
    queue = Queue(name="maintenance", connection=redis)
    try:
        set_operation_status(
            job_id,
            "enqueued",
            "admin-clear",
            progress={"phase": "enqueued", "label": f"Queued clear for {entity}"},
            meta={"entity": entity},
        )
        rq_job = checked_enqueue(
            queue,
            "app.jobs.admin_operations.run_clear_operation",
            entity,
            job_id,
            job_timeout=7200,
            result_ttl=7200,
        )
    except Exception as exc:
        await compensate_operation_enqueue_failure(
            job_id,
            "admin-clear",
            exc,
            redis_client=redis,
        )
        raise
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
    # Never issue Redis KEYS from an HTTP request: even this normally-small
    # namespace shares the RQ server and a growing keyspace would block every
    # queue producer.  SCAN is incremental and MGET keeps the round trips
    # bounded once the matching keys are known.
    keys = list(r.scan_iter(match="admin_operation:*", count=100))
    ops = []
    for raw in (r.mget(keys) if keys else []):
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
        # At the enforced 10% floor a correct 67k-work rebuild may spend many
        # hours yielding between 4 MiB slices.  The generic four-hour timeout
        # would SIGKILL the workhorse before its fenced cleanup can run.
        job_timeout=7 * 24 * 60 * 60,
        # A dedicated coordinator parent shares the import container/cgroup,
        # while normal imports and bounded operations remain available.
        queue_name="maintenance",
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
    ensure_redis_enqueue_capacity(redis)
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    active_job = redis.get("library:rebuild:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            release_owned_operation_lock(
                redis,
                "library:rebuild:active",
                active_job,
            )
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
            queue_name="maintenance",
            progress={"phase": "enqueued", "label": "Library rebuild queued"},
            meta={"entity": "library", **options},
        )
        await task_db.commit()
    try:
        set_operation_status(job_id, "enqueued", "admin-rebuild",
            progress={"phase": "enqueued", "label": "Library rebuild queued"},
            meta={"entity": "library", **options})
        rq_job = checked_enqueue(
            Queue(name="maintenance", connection=redis),
            "app.jobs.admin_operations.run_library_rebuild_operation",
            job_id, options, job_timeout=14400, result_ttl=604800)
    except Exception as exc:
        await compensate_operation_enqueue_failure(
            job_id,
            "admin-rebuild",
            exc,
            lock_key="library:rebuild:active",
            redis_client=redis,
        )
        raise
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
    ensure_redis_enqueue_capacity(redis)
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    active_job = redis.get("library:disk-import:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            release_owned_operation_lock(
                redis,
                "library:disk-import:active",
                active_job,
            )
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
            queue_name="maintenance",
            progress={"phase": "enqueued", "label": "Disk import queued"},
            meta={"entity": "disk-import", **options},
        )
        await task_db.commit()
    try:
        set_operation_status(job_id, "enqueued", "admin-disk-import",
            progress={"phase": "enqueued", "label": "Disk import queued"},
            meta={"entity": "disk-import", **options})
        rq_job = checked_enqueue(
            Queue(name="maintenance", connection=redis),
            "app.jobs.admin_operations.run_disk_import_operation",
            job_id, options, job_timeout=14400, result_ttl=604800)
    except Exception as exc:
        await compensate_operation_enqueue_failure(
            job_id,
            "admin-disk-import",
            exc,
            lock_key="library:disk-import:active",
            redis_client=redis,
        )
        raise
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
    ensure_redis_enqueue_capacity(redis)
    job_id = str(uuid.uuid4())
    active_job = redis.get("library:creator-reenrich:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            release_owned_operation_lock(
                redis,
                "library:creator-reenrich:active",
                active_job,
            )
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
            queue_name="maintenance",
            progress={"phase": "enqueued", "label": "Creator re-enrichment queued"},
            meta={"entity": "creator-reenrich"},
        )
        await task_db.commit()
    try:
        set_operation_status(job_id, "enqueued", "admin-creator-reenrich",
            progress={"phase": "enqueued", "label": "Creator re-enrichment queued"},
            meta={"entity": "creator-reenrich"})
        rq_job = checked_enqueue(
            Queue(name="maintenance", connection=redis),
            "app.jobs.admin_operations.run_creator_reenrich_operation",
            job_id, {}, job_timeout=7200, result_ttl=604800)
    except Exception as exc:
        await compensate_operation_enqueue_failure(
            job_id,
            "admin-creator-reenrich",
            exc,
            lock_key="library:creator-reenrich:active",
            redis_client=redis,
        )
        raise
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Creator re-enrichment queued"}


# ── gallery-dl Config ──
