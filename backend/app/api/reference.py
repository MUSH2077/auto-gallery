import asyncio
import json
import logging
import re
import urllib.parse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from rq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import danbooru as danbooru_svc
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator
import uuid

logger = logging.getLogger(__name__)

def _get_auto_enable_sources() -> set[str]:
    """Read auto-enable-on-import from gallery-dl config.json per extractor."""
    import json, os
    try:
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            extractors = cfg.get("extractor", {})
            result = set()
            for src_name, src_cfg in extractors.items():
                if src_cfg.get("auto-enable-on-import") is True:
                    # Map gallery-dl extractor names to provider source names
                    if src_name == "twitter":
                        result.add("x")
                    else:
                        result.add(src_name)
            if result:
                return result
    except Exception:
        pass
    return {"pixiv"}


def _is_source_auto_enabled(source: str) -> bool:
    return source in _get_auto_enable_sources()


router = APIRouter(dependencies=[RequireAdmin])


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
    from app.models.creator import Creator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource

    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")
    creator_name = data.get("creator_name", "").strip()

    if not source_url and not pixiv_id and not artist_name:
        raise HTTPException(status_code=400, detail="At least one of url, pixiv_id, or name is required")

    artist, links = await asyncio.to_thread(
        danbooru_svc.search_and_extract,
        source_url=source_url, pixiv_id=pixiv_id, artist_name=artist_name,
    )
    if not artist:
        return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}

    # Step 1: Get or create Creator (with dedup check)
    from app.services.creator_dedup import find_existing_creator

    creator_id_str = data.get("creator_id")
    if creator_id_str:
        creator_id = UUID(creator_id_str)
        creator = await db.get(Creator, creator_id)
        if not creator:
            raise HTTPException(status_code=404, detail="Creator not found")
    else:
        # canonical_name: Danbooru tag name (slug-like, lowercase) — used for dedup/identity
        # chosen_display: user-provided pretty name or fallback
        canonical_name = artist.get("name", "Unknown")
        chosen_display = creator_name or canonical_name

        # Extract Pixiv user ID from artist URLs for secondary dedup check
        _pixiv_user_id: str | None = None
        _pixiv_profile_url: str | None = None
        for _u in artist.get("urls", []):
            _raw_url = _u.get("normalized_url") or _u.get("url", "")
            _m = re.search(r'pixiv\.net/(?:en/)?users/(\d+)', _raw_url)
            if _m:
                _pixiv_user_id = _m.group(1)
                _pixiv_profile_url = f"https://www.pixiv.net/users/{_pixiv_user_id}"
                break

        # Three-level dedup: danbooru_artist_id → Pixiv source_creator_id → Pixiv profile URL
        existing = await find_existing_creator(
            db,
            danbooru_artist_id=artist.get("id"),
            source="pixiv" if _pixiv_user_id else None,
            source_creator_id=_pixiv_user_id,
            source_url=_pixiv_profile_url,
        )
        if existing:
            creator = existing
            creator_id = creator.id
            creator_id_str = str(creator_id)
            # Only update display_name if user explicitly chose one
            if creator_name:
                creator.display_name = creator_name
        else:
            creator = Creator(name=canonical_name, display_name=chosen_display)
            db.add(creator)
            await db.flush()
            creator_id = creator.id
            creator_id_str = str(creator_id)

    # Step 2: Enrich Creator with Danbooru data
    if creator.danbooru_artist_id is None:
        creator.danbooru_artist_id = artist.get("id")
    if not creator.description:
        parts = []
        other_names = artist.get("other_names", [])
        if other_names:
            parts.append(f"Danbooru aliases: {', '.join(other_names)}")
        notes = artist.get("notes")
        if notes:
            parts.append(notes)
        if parts:
            creator.description = "\n".join(parts)

    # Step 3: Import all creator links
    links_created = 0
    for link_data in links:
        existing = await db.execute(
            select(CreatorLink).where(
                CreatorLink.creator_id == creator_id,
                CreatorLink.url == link_data["url"],
            )
        )
        if existing.scalar_one_or_none():
            continue
        db.add(CreatorLink(
            creator_id=creator_id,
            url=link_data["url"],
            link_type=link_data["link_type"],
            source=link_data["source"],
            confidence=link_data["confidence"],
            is_verified=link_data["is_verified"],
            notes=link_data.get("notes"),
        ))
        links_created += 1

    # Step 4: Get or create Subscription
    sub_result = await db.execute(
        select(Subscription).where(Subscription.creator_id == creator_id)
    )
    subscription = sub_result.scalar_one_or_none()
    if not subscription:
        from app.services.subscription import get_subscription_defaults
        defaults = await get_subscription_defaults(db)
        subscription = Subscription(creator_id=creator_id, name=creator_name or None,
            sync_interval_hours=defaults["sync_interval_hours"],
            sync_enabled=defaults["sync_enabled"],
            is_active=defaults["is_active"])
        db.add(subscription)
        await db.flush()

    # Step 5: Create SubscriptionSources for downloadable URLs
    sources_created = 0
    for u in artist.get("urls", []):
        raw_url = u.get("normalized_url") or u.get("url", "")
        if not raw_url or not u.get("is_active", True):
            continue
        if not danbooru_svc.is_downloadable_url(raw_url):
            continue
        # Check if source already exists
        existing_ss = await db.execute(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == subscription.id,
                SubscriptionSource.source_url == raw_url,
            )
        )
        if existing_ss.scalar_one_or_none():
            continue
        db.add(SubscriptionSource(
            subscription_id=subscription.id,
            source=danbooru_svc._classify_url(raw_url),
            source_url=raw_url,
            is_enabled=_is_source_auto_enabled(danbooru_svc._classify_url(raw_url)),
        ))
        sources_created += 1

    # Step 6: Create Danbooru subscription source (default disabled)
    danbooru_posts_url = (
        f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(artist['name'])}"
    )
    existing_ds = await db.execute(
        select(SubscriptionSource).where(
            SubscriptionSource.subscription_id == subscription.id,
            SubscriptionSource.source == "danbooru",
        )
    )
    if not existing_ds.scalar_one_or_none():
        db.add(SubscriptionSource(
            subscription_id=subscription.id,
            source="danbooru",
            source_url=danbooru_posts_url,
            is_enabled=False,
        ))
        sources_created += 1

    await db.commit()

    return {
        "status": "ok",
        "found": True,
        "creator_id": creator_id_str,
        "artist_name": artist["name"],
        "links_imported": links_created,
        "sources_created": sources_created,
        "subscription_id": str(subscription.id),
    }


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

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for pid in pixiv_ids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)
    deduped = len(pixiv_ids) - len(unique)
    pixiv_ids = unique

    # Pre-check: which IDs already have creators in the database?
    existing_ids: list[str] = []
    from app.database import async_session
    from app.models.source_creator import SourceCreator
    from app.models.creator import Creator
    from sqlalchemy import select as sa_select
    async with async_session() as precheck_db:
        result = await precheck_db.execute(
            sa_select(SourceCreator.source_creator_id, Creator.name, Creator.id)
            .join(Creator, Creator.id == SourceCreator.creator_id)
            .where(
                SourceCreator.source == "pixiv",
                SourceCreator.source_creator_id.in_(pixiv_ids),
            )
        )
        existing_map = {row[0]: {"name": row[1], "creator_id": str(row[2])} for row in result}
        existing_ids = [pid for pid in pixiv_ids if pid in existing_map]

    r = redis_lib.from_url(settings.redis_url)
    q = Queue(connection=r)

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
        "already_exists": [{"pixiv_id": pid, "creator_name": existing_map[pid]["name"], "creator_id": existing_map[pid]["creator_id"]} for pid in existing_ids],
    }


@router.get("/danbooru/artist/batch-import/status")
async def get_batch_import_status(job_id: str | None = None):
    """Get progress or results of a batch import job.

    Without job_id: returns the most recent batch result from Redis.
    With job_id: fetches RQ job status + Redis progress/result.
    """
    import redis as redis_lib

    r = redis_lib.from_url(settings.redis_url)
    q = Queue(connection=r)

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

    # Check RQ job status if job_id provided
    job_status = None
    if job_id:
        try:
            job = q.fetch_job(job_id)
            if job:
                job_status = job.get_status()
        except Exception:
            pass

    return {
        "status": "completed" if result else ("running" if progress else "unknown"),
        "progress": progress,
        "result": result,
        "job_status": job_status,
    }


@router.post("/danbooru/url-batch-import")
async def url_batch_import_danbooru(data: dict):
    """Enqueue a batch import job for URLs via Danbooru lookup.

    Accepts: {urls: ["https://pixiv.net/users/123", ...]}
    Returns immediately with a job_id. Poll GET /danbooru/artist/batch-import/status
    for progress and results.
    """
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
    q = Queue(connection=r)

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
