import asyncio
import json
import logging
import uuid
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequirePermission
from rq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import danbooru as danbooru_svc
from app.services.danbooru_import import import_all_danbooru_artist
from app.services.operations import set_operation_status
from app.services.redis_client import get_redis
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[RequirePermission("subscriptions")])


@router.get("/danbooru/artist/{artist_id}")
async def get_danbooru_artist(artist_id: int):
    """Get Danbooru artist detail by ID (for alias chips on Creator Detail page)."""
    from app.services.reference import ReferenceService
    try:
        return await ReferenceService().get_danbooru_artist(artist_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/providers")
async def list_reference_providers():
    return {"providers": [{"source_name": "danbooru_reference", "display_name": "Danbooru", "capabilities": {"is_reference_only": True}}]}


@router.post("/danbooru/artist/preview")
async def preview_danbooru_artist(data: dict, db: AsyncSession = Depends(get_db)):
    """Search Danbooru for artists matching a Pixiv URL, artist name, or tag."""
    from app.services.reference import ReferenceService
    return await ReferenceService(db).preview_danbooru(data)


@router.post("/danbooru/artist/import")
async def import_danbooru_artist(data: dict, db: AsyncSession = Depends(get_db)):
    """Import Danbooru artist reference data as creator links."""
    from app.services.reference import ReferenceService
    creator_id = data.get("creator_id")
    if not creator_id:
        raise HTTPException(status_code=400, detail="creator_id is required")
    try:
        return await ReferenceService(db).import_danbooru_links(UUID(creator_id), data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/danbooru/artist/import-all")
async def import_all_danbooru(data: dict, db: AsyncSession = Depends(get_db)):
    """One-click import: create Creator + Subscription + Sources + Links from Danbooru.

    Accepts: {creator_id?: UUID, name?: str, url?: str, pixiv_id?: str}
    If no creator_id, creates a new Creator. Always creates Subscription + Sources.
    """
    return await import_all_danbooru_artist(data, db)


@router.post("/danbooru/artist/import-all/async")
async def import_all_danbooru_async(data: dict):
    """Enqueue one-click Danbooru import and return a pollable operation id."""
    from app.services.reference import ReferenceService
    try:
        return ReferenceService.enqueue_async_import(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _precheck_pixiv_ids(pixiv_ids: list[str]) -> dict:
    from app.services.reference import ReferenceService
    return await ReferenceService.precheck_pixiv_ids(pixiv_ids)
@router.post("/danbooru/artist/batch-import/preview")
async def preview_batch_import(data: dict):
    """Pre-check Pixiv IDs without enqueuing.

    Accepts: {pixiv_ids: ["1980643", ...]}
    Returns dedup + local-existence info. No Danbooru API calls.
    """
    pixiv_ids = data.get("pixiv_ids", [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        raise HTTPException(status_code=400, detail="pixiv_ids list is required")
    pixiv_ids = [str(pid).strip() for pid in pixiv_ids if str(pid).strip()]
    if not pixiv_ids:
        raise HTTPException(status_code=400, detail="No valid pixiv_ids provided")
    precheck = await _precheck_pixiv_ids(pixiv_ids)
    return {k: v for k, v in precheck.items() if k not in ("unique_ids", "existing_ids")}


@router.post("/danbooru/artist/batch-import")
async def batch_import_danbooru_artists(data: dict):
    """Enqueue a batch import job for Pixiv user IDs via Danbooru.

    Accepts: {pixiv_ids: ["1980643", "123456", ...]}
    Returns immediately with a job_id. Poll GET /danbooru/artist/batch-import/status
    for progress and results.
    """
    import redis as redis_lib

    pixiv_ids = data.get("pixiv_ids", [])
    if not pixiv_ids or not isinstance(pixiv_ids, list):
        raise HTTPException(status_code=400, detail="pixiv_ids list is required")
    pixiv_ids = [str(pid).strip() for pid in pixiv_ids if str(pid).strip()]
    if not pixiv_ids:
        raise HTTPException(status_code=400, detail="No valid pixiv_ids provided")

    precheck = await _precheck_pixiv_ids(pixiv_ids)
    deduped = precheck["duplicates_removed"]
    existing_ids = precheck["existing_ids"]
    # Filter out creators that already exist in the database.
    # The precheck identified them via SourceCreator records — skip them
    # instead of making unnecessary Danbooru API calls for every one.
    pixiv_ids = [pid for pid in precheck["unique_ids"] if pid not in existing_ids]
    skipped_existing = len(existing_ids)

    if not pixiv_ids:
        return {
            "status": "ok",
            "message": f"All {len(precheck['unique_ids'])} IDs already exist in the library — nothing to import",
            "job_id": None,
            "total": 0,
            "duplicates_removed": deduped,
            "already_exists": precheck["already_exists"],
            "skipped_existing": skipped_existing,
        }

    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="imports", connection=r)

    # Generate key before enqueue so it can be passed to the RQ function AND
    # returned as job_id. This ensures Redis keys match the polling ID.
    job_key = str(uuid.uuid4())

    job = q.enqueue("app.jobs.batch_import.run_batch_import", pixiv_ids, job_key,
                    job_timeout=3600,  # 1 hour max
                    result_ttl=3600)

    logger.info("Enqueued batch import job_key=%s (rq_job=%s) with %d new pixiv_ids (%d duplicates removed, %d already exist — skipped)",
                job_key, job.id, len(pixiv_ids), deduped, skipped_existing)
    return {
        "status": "ok",
        "message": (
            f"Batch import enqueued ({len(pixiv_ids)} new IDs"
            + (f", {deduped} duplicates removed" if deduped > 0 else "")
            + (f", {skipped_existing} already exist — skipped" if skipped_existing > 0 else "")
            + ")"
        ),
        "job_id": job_key,
        "total": len(pixiv_ids),
        "duplicates_removed": deduped,
        "already_exists": precheck["already_exists"],
        "skipped_existing": skipped_existing,
    }


@router.get("/danbooru/artist/batch-import/status")
async def get_batch_import_status(job_id: str | None = None):
    """Get progress or results of a batch import job.

    Without job_id: returns the most recent batch result from Redis.
    With job_id: fetches RQ job status + Redis progress/result.
    """
    import redis as redis_lib

    r = redis_lib.from_url(settings.redis_url)

    # Check for in-progress data (scoped by job_id for concurrent imports)
    if job_id:
        progress_raw = r.get(f"batch_import:{job_id}:progress")
        result_raw = r.get(f"batch_import:{job_id}:result")
    else:
        progress_raw = r.get("batch_import:progress")
        result_raw = r.get("batch_import:result")

    progress = None
    if progress_raw:
        try:
            progress = json.loads(progress_raw)
        except Exception:
            pass

    result = None
    if result_raw:
        try:
            result = json.loads(result_raw)
        except Exception:
            pass

    # Check RQ job status if job_id provided — try all active queues
    job_status = None
    if job_id:
        try:
            from rq import Queue
            for qname in ("imports", "default", "downloads", "scheduled"):
                q = Queue(name=qname, connection=r)
                job = q.fetch_job(job_id)
                if job:
                    job_status = job.get_status()
                    break
        except Exception:
            pass

    # If the job is still queued/started (RQ knows about it), it's running
    if job_status and job_status in ("queued", "started", "scheduled"):
        progress = progress or {}

    return {
        "status": "completed" if result else ("running" if progress or (job_status in ("queued", "started", "scheduled")) else "unknown"),
        "progress": progress,
        "result": result,
        "job_status": job_status,
    }


@router.post("/danbooru/url-batch-import/preview")
async def preview_url_batch_import(data: dict):
    """Pre-check URLs without enqueuing.

    Accepts: {urls: ["https://pixiv.net/users/123", ...]}
    Returns dedup info only. No Danbooru API calls.
    """
    urls = data.get("urls", [])
    if not urls or not isinstance(urls, list):
        raise HTTPException(status_code=400, detail="urls list is required")
    urls = [str(u).strip() for u in urls if str(u).strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")

    seen: set[str] = set()
    unique: list[str] = []
    duplicate_urls: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
        else:
            duplicate_urls.append(u)

    return {
        "total": len(urls),
        "unique_count": len(unique),
        "duplicates_removed": len(duplicate_urls),
        "duplicate_urls": duplicate_urls,
    }


@router.post("/danbooru/url-batch-import")
async def url_batch_import_danbooru(data: dict):
    """Enqueue a batch import job for URLs via Danbooru lookup.

    Accepts: {urls: ["https://pixiv.net/users/123", ...]}
    Returns immediately with a job_id. Poll GET /danbooru/artist/batch-import/status
    for progress and results.
    """
    import redis as redis_lib
    import uuid as uuid_mod

    urls = data.get("urls", [])
    if not urls or not isinstance(urls, list):
        raise HTTPException(status_code=400, detail="urls list is required")
    urls = [str(u).strip() for u in urls if str(u).strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")
    if len(urls) > 100:
        raise HTTPException(status_code=400, detail="Too many URLs (max 100 per request)")

    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="imports", connection=r)

    # Generate key before enqueue so Redis keys match the polling ID
    job_key = str(uuid_mod.uuid4())

    job = q.enqueue("app.jobs.batch_import.run_url_batch_import", urls, job_key,
                    job_timeout=3600,
                    result_ttl=3600)

    logger.info("Enqueued URL batch import job_key=%s (rq_job=%s) with %d URLs",
                job_key, job.id, len(urls))
    return {
        "status": "ok",
        "message": f"URL batch import enqueued ({len(urls)} URLs)",
        "job_id": job_key,
        "total": len(urls),
    }
