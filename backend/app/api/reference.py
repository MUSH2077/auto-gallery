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
    artist = await asyncio.to_thread(danbooru_svc.get_artist, artist_id)
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found on Danbooru")
    pixiv_display_name = None
    from app.database import async_session
    from app.models.source_creator import SourceCreator
    from sqlalchemy import select as sa_select
    pixiv_ids = []
    for u in artist.get("urls", []):
        raw = u.get("normalized_url") or u.get("url", "")
        m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', raw)
        if m: pixiv_ids.append(m.group(1))
    if pixiv_ids:
        async with async_session() as db:
            result = await db.execute(
                sa_select(SourceCreator).where(
                    SourceCreator.source == "pixiv",
                    SourceCreator.source_creator_id.in_(pixiv_ids),
                ).limit(1))
            sc = result.scalar_one_or_none()
            if sc and sc.display_name:
                pixiv_display_name = sc.display_name
    return {
        "artist": {
            "id": artist["id"],
            "name": artist["name"],
            "other_names": artist.get("other_names", []),
            "pixiv_display_name": pixiv_display_name,
        }
    }


@router.get("/providers")
async def list_reference_providers():
    return {"providers": [{"source_name": "danbooru_reference", "display_name": "Danbooru", "capabilities": {"is_reference_only": True}}]}


@router.post("/danbooru/artist/preview")
async def preview_danbooru_artist(data: dict, db: AsyncSession = Depends(get_db)):
    """Search Danbooru for artists matching a Pixiv URL, artist name, or tag.

    Accepts: {url?: str, pixiv_id?: str, name?: str}
    Returns pixiv_display_name when a matching SourceCreator is found in the local DB.
    """
    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")

    if not source_url and not pixiv_id and not artist_name:
        return {"status": "error", "message": "At least one of url, pixiv_id, or name is required"}

    artist, links = await asyncio.to_thread(
        danbooru_svc.search_and_extract,
        source_url=source_url,
        pixiv_id=pixiv_id,
        artist_name=artist_name,
    )

    if not artist:
        return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}

    # Resolve Pixiv user ID from search params or artist URLs
    pixiv_user_id: str | None = None
    if pixiv_id:
        pixiv_user_id = str(pixiv_id).strip()
    elif source_url:
        m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', source_url)
        if m:
            pixiv_user_id = m.group(1)
    # Also check the artist's associated URLs as a fallback
    if not pixiv_user_id:
        for u in artist.get("urls", []):
            raw = u.get("normalized_url") or u.get("url", "")
            m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', raw)
            if m:
                pixiv_user_id = m.group(1)
                break

    # Look up Pixiv display name from existing SourceCreator records
    pixiv_display_name: str | None = None
    if pixiv_user_id:
        result = await db.execute(
            select(SourceCreator).where(
                SourceCreator.source == "pixiv",
                SourceCreator.source_creator_id == pixiv_user_id,
            ).limit(1)
        )
        sc = result.scalar_one_or_none()
        if sc and sc.display_name:
            pixiv_display_name = sc.display_name

    return {
        "status": "ok",
        "found": True,
        "artist": {
            "id": artist["id"],
            "name": artist["name"],
            "other_names": artist.get("other_names", []),
            "post_count": artist.get("post_count"),
            "notes": artist.get("notes"),
            "is_active": artist.get("is_active"),
            "created_at": artist.get("created_at"),
            "urls": [{"url": u["url"], "normalized_url": u.get("normalized_url", u["url"]),
                      "is_active": u.get("is_active", True)} for u in artist.get("urls", [])],
            "pixiv_display_name": pixiv_display_name,
        },
        "suggested_links": links,
    }


@router.post("/danbooru/artist/import")
async def import_danbooru_artist(data: dict, db: AsyncSession = Depends(get_db)):
    """Import Danbooru artist reference data as creator links.

    Accepts: {creator_id: UUID, url?: str, pixiv_id?: str, name?: str}
    Creates CreatorLink records for the found artist.
    """
    creator_id = data.get("creator_id")
    if not creator_id:
        raise HTTPException(status_code=400, detail="creator_id is required")

    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")

    if not source_url and not pixiv_id and not artist_name:
        raise HTTPException(status_code=400, detail="At least one of url, pixiv_id, or name is required")

    artist, links = await asyncio.to_thread(
        danbooru_svc.search_and_extract,
        source_url=source_url,
        pixiv_id=pixiv_id,
        artist_name=artist_name,
    )

    if not artist or not links:
        return {"status": "ok", "imported": 0, "message": "No matching Danbooru artist found"}

    created = 0
    for link_data in links:
        db.add(CreatorLink(
            creator_id=UUID(creator_id),
            url=link_data["url"],
            link_type=link_data["link_type"],
            source=link_data["source"],
            confidence=link_data["confidence"],
            is_verified=link_data["is_verified"],
            notes=link_data.get("notes"),
        ))
        created += 1

    await db.commit()
    return {"status": "ok", "imported": created, "artist_name": artist["name"]}


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
    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")
    if not source_url and not pixiv_id and not artist_name:
        raise HTTPException(status_code=400, detail="At least one of url, pixiv_id, or name is required")

    job_id = str(uuid.uuid4())
    set_operation_status(
        job_id,
        "queued",
        "danbooru-import-all",
        progress={"phase": "queued", "label": "Queued Danbooru import"},
        meta={"name": artist_name, "pixiv_id": pixiv_id},
    )
    queue = Queue(name="imports", connection=get_redis())
    rq_job = queue.enqueue(
        "app.jobs.danbooru_import.run_import_all_danbooru",
        data,
        job_id,
        job_timeout=3600,
        result_ttl=7200,
    )
    logger.info("Enqueued Danbooru import-all operation job_id=%s rq_job=%s", job_id, rq_job.id)
    return {"job_id": job_id, "status": "queued"}


async def _precheck_pixiv_ids(pixiv_ids: list[str]) -> dict:
    """Deduplicate Pixiv IDs and check which already exist in the local DB.

    Returns metadata dict — does NOT enqueue anything.
    """
    total = len(pixiv_ids)
    seen: set[str] = set()
    unique: list[str] = []
    duplicate_ids: list[str] = []
    for pid in pixiv_ids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)
        else:
            duplicate_ids.append(pid)

    from app.database import async_session
    from app.models.source_creator import SourceCreator
    from app.models.creator import Creator
    from sqlalchemy import select as sa_select
    async with async_session() as db:
        result = await db.execute(
            sa_select(SourceCreator.source_creator_id, Creator.name, Creator.id)
            .join(Creator, Creator.id == SourceCreator.creator_id)
            .where(
                SourceCreator.source == "pixiv",
                SourceCreator.source_creator_id.in_(unique),
            )
        )
        existing_map = {row[0]: {"name": row[1], "creator_id": str(row[2])} for row in result}
        existing_ids = [pid for pid in unique if pid in existing_map]

    return {
        "total": total,
        "unique_count": len(unique),
        "duplicates_removed": len(duplicate_ids),
        "duplicate_ids": duplicate_ids,
        "already_exists": [
            {"pixiv_id": pid, "creator_name": existing_map[pid]["name"],
             "creator_id": existing_map[pid]["creator_id"]}
            for pid in existing_ids
        ],
        "new_count": len(unique) - len(existing_ids),
        "unique_ids": unique,
        "existing_ids": existing_ids,
    }


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
    pixiv_ids = precheck["unique_ids"]
    deduped = precheck["duplicates_removed"]
    existing_ids = precheck["existing_ids"]

    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="imports", connection=r)

    # Generate key before enqueue so it can be passed to the RQ function AND
    # returned as job_id. This ensures Redis keys match the polling ID.
    job_key = str(uuid.uuid4())

    job = q.enqueue("app.jobs.batch_import.run_batch_import", pixiv_ids, job_key,
                    job_timeout=3600,  # 1 hour max
                    result_ttl=3600)

    logger.info("Enqueued batch import job_key=%s (rq_job=%s) with %d pixiv_ids (%d duplicates removed, %d already exist)",
                job_key, job.id, len(pixiv_ids), deduped, len(existing_ids))
    return {
        "status": "ok",
        "message": f"Batch import enqueued ({len(pixiv_ids)} IDs" + (f", {deduped} duplicates removed)" if deduped > 0 else ")"),
        "job_id": job_key,
        "total": len(pixiv_ids),
        "duplicates_removed": deduped,
        "already_exists": precheck["already_exists"],
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
