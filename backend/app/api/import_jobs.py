import logging
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequirePermission, get_admin_key
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, String, or_

from app.database import get_db
from app.models.import_job import ImportJob
from app.models.download_job import DownloadJob
from app.models.subscription import Subscription
from app.models.creator import Creator
from app.schemas.import_job import ImportJobRead
from app.models.task_state import transition_import_job
from app.services.job_progress import import_progress_from_job
from app.services.progress import ProgressTracker
from app.services.task_engine import TaskEngine, TaskEngineError

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[RequirePermission("tasks")])


def _import_job_payload(job: ImportJob) -> dict:
    payload = ImportJobRead.model_validate(job).model_dump(mode="json")
    progress = ProgressTracker.get(str(job.id))
    if progress:
        payload["progress_data"] = progress
    else:
        payload["progress_data"] = import_progress_from_job(job)
    return payload


async def _enrich_import_context(db: AsyncSession, jobs: list[ImportJob]) -> dict:
    """Resolve parent download-job context (source/url/subscription/creator) for a
    batch of import jobs so the import list can show the same info as downloads."""
    dj_ids = {j.download_job_id for j in jobs if j.download_job_id}
    if not dj_ids:
        return {}
    dj_rows = (await db.execute(
        select(DownloadJob.id, DownloadJob.source, DownloadJob.source_url, DownloadJob.subscription_id)
        .where(DownloadJob.id.in_(dj_ids))
    )).all()
    dj = {r.id: r for r in dj_rows}

    sub_ids = {r.subscription_id for r in dj_rows if r.subscription_id}
    sub_ctx: dict = {}
    if sub_ids:
        sub_rows = (await db.execute(
            select(Subscription.id, Subscription.name, Subscription.creator_id,
                   Creator.display_name, Creator.name)
            .join(Creator, Creator.id == Subscription.creator_id)
            .where(Subscription.id.in_(sub_ids))
        )).all()
        sub_ctx = {
            sid: {
                "subscription_name": sname,
                "creator_id": str(cid) if cid else None,
                "creator_name": cdisplay or cname,
            }
            for sid, sname, cid, cdisplay, cname in sub_rows
        }

    ctx: dict = {}
    for j in jobs:
        d = dj.get(j.download_job_id)
        if not d:
            continue
        s = sub_ctx.get(d.subscription_id, {})
        ctx[j.id] = {
            "source": d.source,
            "source_url": d.source_url,
            "subscription_id": str(d.subscription_id) if d.subscription_id else None,
            "subscription_name": s.get("subscription_name"),
            "creator_id": s.get("creator_id"),
            "creator_name": s.get("creator_name"),
        }
    return ctx


@router.post("/scan")
async def scan_imports(db: AsyncSession = Depends(get_db)):
    """Scan for pending import work: check downloads dir for unprocessed JSON metadata files."""
    import os
    from pathlib import Path
    from app.config import settings

    job_count = 0
    # Find all download dirs that have metadata JSONs from --write-metadata
    dl_root = Path(settings.download_root)
    if dl_root.exists():
        for source_dir in dl_root.iterdir():
            if not source_dir.is_dir():
                continue
            for user_dir in source_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                for work_dir in user_dir.iterdir():
                    if not work_dir.is_dir():
                        continue
                    json_files = list(work_dir.glob("*.json"))
                    if json_files:
                        job_count += 1

    # Also count existing failed import jobs that can be retried
    failed = await db.execute(
        select(ImportJob).where(ImportJob.status.in_(["failed", "stale"]))
    )
    failed_count = len(failed.scalars().all())

    return {
        "status": "ok",
        "message": f"Found {job_count} directories with pending metadata, {failed_count} retryable jobs",
        "pending_dirs": job_count,
        "retryable_jobs": failed_count,
    }


@router.get("")
async def list_import_jobs(
    status: str | None = None,
    download_job_id: UUID | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ImportJob)
    if status:
        stmt = stmt.where(ImportJob.status == status)
    if download_job_id:
        stmt = stmt.where(ImportJob.download_job_id == download_job_id)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            cast(ImportJob.id, String).ilike(pattern),
            cast(ImportJob.download_job_id, String).ilike(pattern),
            ImportJob.error_log.ilike(pattern),
        ))
    count_stmt = select(func.count(ImportJob.id))
    if status:
        count_stmt = count_stmt.where(ImportJob.status == status)
    if download_job_id:
        count_stmt = count_stmt.where(ImportJob.download_job_id == download_job_id)
    if q:
        pattern = f"%{q.strip()}%"
        count_stmt = count_stmt.where(or_(
            cast(ImportJob.id, String).ilike(pattern),
            cast(ImportJob.download_job_id, String).ilike(pattern),
            ImportJob.error_log.ilike(pattern),
        ))
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()
    stmt = stmt.order_by(ImportJob.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())
    ctx = await _enrich_import_context(db, jobs)
    items = []
    for j in jobs:
        payload = _import_job_payload(j)
        payload.update(ctx.get(j.id, {}))
        items.append(payload)
    return {"total": total, "items": items}


@router.get("/{job_id}")
async def get_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImportJob).where(ImportJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return _import_job_payload(job)


@router.post("/{job_id}/retry")
async def retry_import_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    engine = TaskEngine(db)
    try:
        result = await engine.retry_import(job_id, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{job_id}")
async def delete_import_job(job_id: UUID, db: AsyncSession = Depends(get_db)):
    engine = TaskEngine(db)
    try:
        await engine.delete_import(job_id)
        return {"status": "ok"}
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ── Task Engine endpoints ─────────────────────────────────────────


@router.post("/{job_id}/cancel")
async def cancel_import_job(
    job_id: UUID,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    engine = TaskEngine(db)
    try:
        note = data.get("note") if data else None
        result = await engine.cancel_import(job_id, note=note, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{job_id}/priority")
async def set_import_priority(
    job_id: UUID,
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    priority = data.get("priority", 10)
    engine = TaskEngine(db)
    try:
        result = await engine.set_priority_import(job_id, priority, operator=operator)
        await db.commit()
        return result
    except TaskEngineError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/batch-by-filter")
async def batch_import_by_filter(
    data: dict,
    db: AsyncSession = Depends(get_db),
    operator: str = Depends(get_admin_key),
):
    filters = data.get("filters", {})
    action = data.get("action", "")
    note = data.get("note")
    engine = TaskEngine(db)
    try:
        return await engine.batch_by_filter("import", filters, action, operator=operator, note=note)
    except TaskEngineError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{job_id}/progress")
async def get_import_progress(job_id: UUID):
    progress = ProgressTracker.get(str(job_id))
    if not progress:
        raise HTTPException(status_code=404, detail="No progress data available")
    return {"job_id": str(job_id), **progress}
