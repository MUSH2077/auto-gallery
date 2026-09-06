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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.services.redis_client import get_redis
from app.services.operations import get_operation_status
from app.services.settings import source_key_for_extractor
from app.schemas.admin_operations import AdminOperationAccepted
from app.services.admin_data import CONFIRMATION_PHRASES, preview_clear_entity_data

from ._routers import _clear_files, router


DEFAULT_DEDUP = {
    "auto_group_enabled": True,
    "phash_threshold": 4,
    "ssim_threshold": 0.98,
    "aspect_ratio_tolerance": 0.01,
    "auto_group_score": 95,
    "review_score": 70,
    "quarantine_days": 30,
}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False, "auto_resolve_upstream_conflicts": True}


# ── Settings ──




@router.post("/cleanup-metadata-jsons", status_code=202)
async def cleanup_metadata_jsons():
    """Remove all gallery-dl metadata JSON files from downloads directory."""
    from app.services.operations import enqueue_admin_operation

    return await enqueue_admin_operation(
        lock_key="library:cleanup-metadata-jsons:active",
        operation_type="admin-cleanup-metadata-jsons",
        title="Clean metadata JSON files",
        entity="metadata-jsons",
        func="app.jobs.admin_operations.run_cleanup_metadata_jsons_operation",
        options={},
        job_timeout=7200,
        queue_name="maintenance",
    )


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



class BackupScheduleRequest(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=24, ge=1, le=8760)


@router.post("/backup/schedule")
async def schedule_backup(
    data: BackupScheduleRequest,
    db: AsyncSession = Depends(get_db),
):
    """Persist recurring backup intent in PostgreSQL."""
    from app.models.system_setting import SystemSetting

    now = datetime.now(timezone.utc)
    value = {
        "enabled": data.enabled,
        "interval_hours": data.interval_hours,
        "next_run_at": (
            now + timedelta(hours=data.interval_hours)
        ).isoformat() if data.enabled else None,
    }
    row = await db.get(SystemSetting, "backup_schedule")
    if row is None:
        db.add(SystemSetting(key="backup_schedule", value=value))
    else:
        row.value = value
    await db.commit()
    if data.enabled:
        return {
            "status": "ok",
            "message": f"Auto-backup enabled, every {data.interval_hours}h",
        }
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

@router.post("/clear/{entity}", status_code=202)
async def clear_entity(entity: ClearEntity, data: ClearOperationRequest, db: AsyncSession = Depends(get_db)):
    """Compatibility route for the registered asynchronous clear operation."""
    if data.entity != entity:
        raise HTTPException(status_code=422, detail="Path and request entity do not match")
    _validate_clear_confirmation(data)
    from app.services.operations import enqueue_admin_operation

    await db.rollback()
    return await enqueue_admin_operation(
        lock_key=f"library:clear:{entity}",
        operation_type="admin-clear",
        title=f"Clear {entity}",
        entity=entity,
        func="app.jobs.admin_operations.run_clear_operation",
        options={"entity": entity},
        job_timeout=7200,
        queue_name="maintenance",
    )


@router.post("/operations/clear", status_code=202)
async def start_clear_operation(data: ClearOperationRequest, db: AsyncSession = Depends(get_db)):
    """Enqueue a data-management clear operation and return immediately."""
    from app.services.operations import enqueue_admin_operation

    _validate_clear_confirmation(data)
    entity = data.entity
    await db.rollback()
    return await enqueue_admin_operation(
        lock_key=f"library:clear:{entity}",
        operation_type="admin-clear",
        title=f"Clear {entity}",
        entity=entity,
        func="app.jobs.admin_operations.run_clear_operation",
        options={"entity": entity},
        job_timeout=7200,
        queue_name="maintenance",
    )


@router.get("/operations/{job_id}")
async def get_admin_operation(job_id: str):
    from app.services.tasks import TaskService, task_payload

    task_id_text = job_id
    if job_id.startswith("admin-") and "-attempt-" in job_id:
        task_id_text = job_id.removeprefix("admin-").rsplit("-attempt-", 1)[0]
    try:
        task_id = UUID(task_id_text)
    except ValueError:
        task_id = None
    if task_id is not None:
        async with async_session() as db:
            task = await TaskService(db).get(task_id)
            if task and task.kind == "admin":
                payload = task_payload(task)
                return {
                    "task_id": payload["id"],
                    # Keep the historical logical-id field for polling clients;
                    # the durable RQ transport id is explicit below.
                    "job_id": payload["id"],
                    "rq_job_id": payload["rq_job_id"],
                    "status": payload["status"],
                    "operation_type": payload["operation_type"],
                    "progress": payload["progress_data"],
                    "result": payload["result_data"],
                    "error": payload["error_log"],
                    "reason_code": payload["reason_code"],
                    "meta": payload["meta"],
                    "updated_at": task.updated_at.timestamp() if task.updated_at else None,
                }

    # One-version, read-only compatibility for operations created before the
    # TaskRun registry existed. Redis never participates in current authority.
    try:
        status = get_operation_status(job_id)
    except Exception:
        status = None
    if status:
        return status
    raise HTTPException(status_code=404, detail="Operation not found")


@router.post(
    "/operations/{task_id}/retry",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def retry_registered_admin_operation(task_id: UUID):
    """Retry a failed registered administrator operation on the same TaskRun."""
    from app.services.operations import retry_admin_operation

    return await retry_admin_operation(task_id)


@router.get("/operations")
async def list_active_operations():
    """List all active (running + enqueued) admin operations."""
    from app.models.task_run import TaskRun
    from app.services.tasks import task_payload

    async with async_session() as db:
        tasks = list(
            (
                await db.execute(
                    select(TaskRun)
                    .where(
                        TaskRun.kind == "admin",
                        TaskRun.status.in_({"enqueued", "running", "recovering", "paused"}),
                    )
                    .order_by(TaskRun.created_at.desc())
                    .limit(100)
                )
            ).scalars()
        )
        ops = []
        for task in tasks:
            payload = task_payload(task)
            ops.append(
                {
                    "task_id": payload["id"],
                    "job_id": payload["id"],
                    "rq_job_id": payload["rq_job_id"],
                    "status": payload["status"],
                    "operation_type": payload["operation_type"],
                    "progress": payload["progress_data"],
                    "result": payload["result_data"],
                    "error": payload["error_log"],
                    "meta": payload["meta"],
                    "updated_at": task.updated_at.timestamp() if task.updated_at else None,
                }
            )

    known = {item["job_id"] for item in ops}
    # Merge legacy Redis-only rows after the authoritative query. Transport
    # outage merely removes the compatibility tail; PostgreSQL results remain.
    try:
        r = get_redis()
        keys = list(r.scan_iter(match="admin_operation:*", count=100))
        for raw_key in keys:
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
            legacy_id = key.removeprefix("admin_operation:")
            if legacy_id in known:
                continue
            payload = get_operation_status(legacy_id, redis_client=r)
            if payload and payload.get("status") in ("queued", "enqueued", "running"):
                ops.append(payload)
    except Exception:
        pass
    ops.sort(key=lambda o: o.get("updated_at", 0), reverse=True)
    return {"operations": ops}

@router.post("/search/reindex", status_code=202)
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


@router.post(
    "/data/creator-aliases/backfill",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def backfill_creator_aliases_operation():
    """Enqueue the resumable projection of stored creator identity evidence."""

    from app.services.operations import enqueue_admin_operation

    return await enqueue_admin_operation(
        lock_key="library:creator-alias-backfill:active",
        operation_type="admin-creator-alias-backfill",
        title="Backfill creator identity aliases",
        entity="creator-aliases",
        func="app.jobs.admin_operations.run_creator_alias_backfill_operation",
        options={},
        job_timeout=7200,
        queue_name="maintenance",
    )


# ── Library ──

class RebuildLibraryRequest(BaseModel):
    mode: Literal["repair", "full"] = "repair"
    source: str | None = None
    creator_id: UUID | None = None
    work_id: UUID | None = None
    resume: bool = True


@router.post("/library/rebuild", status_code=202)
async def rebuild_library(data: RebuildLibraryRequest | None = None):
    """Enqueue a library rebuild operation and return immediately."""
    from app.services.operations import enqueue_admin_operation

    options = (data or RebuildLibraryRequest()).model_dump(mode="json")
    operation = await enqueue_admin_operation(
        lock_key="library:rebuild:active",
        operation_type="admin-rebuild",
        title="Rebuild library index",
        entity="library",
        func="app.jobs.admin_operations.run_library_rebuild_operation",
        options=options,
        job_timeout=14400,
        queue_name="maintenance",
    )
    return {**operation, "message": "Library rebuild queued", "options": options}


class ImportFromDiskRequest(BaseModel):
    source: str | None = None
    repository_id: UUID | None = None
    # Reprocess every on-disk file even if the ledger marks it 'done'. Use this
    # to recover creators/works after they were deleted from the DB (the ledger
    # is not reset by deletion, so a normal disk-import skips them as 'done').
    reset_ledger: bool = False


@router.post("/library/import-from-disk", status_code=202)
async def import_from_disk(
    data: ImportFromDiskRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Enqueue an idempotent import of on-disk download files into the DB."""
    from app.services.operations import enqueue_admin_operation

    request = data or ImportFromDiskRequest()
    if request.repository_id is not None:
        from app.models.subscription_source import SubscriptionSource

        repository = await db.get(SubscriptionSource, request.repository_id)
        if repository is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_repository_id", "message": "repository_id does not reference a repository"},
            )
        if (
            request.source
            and source_key_for_extractor(request.source)
            != source_key_for_extractor(repository.source)
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "repository_source_mismatch", "message": "source does not match repository source"},
            )
    options = request.model_dump(mode="json")
    # Repository validation is complete; release its read transaction before
    # any Redis admission or RQ publication can wait on external state.
    await db.rollback()
    operation = await enqueue_admin_operation(
        lock_key="library:disk-import:active",
        operation_type="admin-disk-import",
        title="Import from disk",
        entity="disk-import",
        func="app.jobs.admin_operations.run_disk_import_operation",
        options=options,
        job_timeout=14400,
        queue_name="maintenance",
    )
    return {**operation, "message": "Disk import queued", "options": options}


@router.post("/creators/re-enrich", status_code=202)
async def reenrich_creators():
    """Enqueue a Danbooru re-enrichment sweep for creators flagged needs_enrichment."""
    from app.services.operations import enqueue_admin_operation

    operation = await enqueue_admin_operation(
        lock_key="library:creator-reenrich:active",
        operation_type="admin-creator-reenrich",
        title="Re-enrich creators from Danbooru",
        entity="creator-reenrich",
        func="app.jobs.admin_operations.run_creator_reenrich_operation",
        options={},
        job_timeout=7200,
        queue_name="maintenance",
    )
    return {**operation, "message": "Creator re-enrichment queued"}


# ── gallery-dl Config ──
