"""RQ job: batch import Pixiv user IDs and URLs via Danbooru one-click flow.

Runs asynchronously in the RQ worker so it can handle large batches without
HTTP request timeouts. Redis keys are scoped by job_id to support concurrent imports.
"""

import asyncio
import json
import logging
from app.services.redis_client import get_redis
from app.database import async_session
from app.services.cache import invalidate_creator_subscription_caches

logger = logging.getLogger(__name__)

PROGRESS_TTL = 3600   # 1 hour for in-progress data (matches RQ job_timeout)
RESULT_TTL = 7200     # 2 hours for completed results


def _redis_keys(job_id: str) -> tuple[str, str]:
    """Return (progress_key, result_key) scoped to this job."""
    return (f"batch_import:{job_id}:progress", f"batch_import:{job_id}:result")


async def _publish_progress(job_id: str, progress: dict, redis_client) -> None:
    """Fence registered progress in PostgreSQL; retain legacy Redis transport."""

    from app.services.operations import (
        current_admin_operation_attempt,
        update_current_admin_operation_progress,
    )

    delivery = current_admin_operation_attempt()
    if delivery is not None and str(delivery[0]) == str(job_id):
        await update_current_admin_operation_progress(
            job_id,
            {"phase": "running", **progress},
        )
        return
    progress_key, _result_key = _redis_keys(job_id)
    redis_client.setex(progress_key, PROGRESS_TTL, json.dumps(progress))


def run_batch_import(pixiv_ids: list[str], job_id: str = ""):
    """Entry point for RQ worker — Pixiv ID batch import."""
    return asyncio.run(_batch_import(pixiv_ids, job_id))


def run_url_batch_import(urls: list[str], job_id: str = ""):
    """Entry point for RQ worker — URL batch import."""
    return asyncio.run(_url_batch_import(urls, job_id))


async def _batch_import(pixiv_ids: list[str], job_id: str) -> dict:
    """Process a batch of Pixiv user IDs: search Danbooru, auto-import."""
    from app.services import danbooru as danbooru_svc
    from app.services.danbooru_import import persist_danbooru_artist

    imported = []
    low_confidence = []
    not_found = []
    errors = []

    total = len(pixiv_ids)
    from app.services.operations import (
        AdminOperationAttemptRejected,
        current_admin_operation_attempt,
        fence_current_admin_operation_transaction,
    )

    delivery = current_admin_operation_attempt()
    r = None if delivery is not None else get_redis()
    progress_key, result_key = _redis_keys(job_id)

    async with async_session() as db:
        for idx, pid in enumerate(pixiv_ids):
            pid = str(pid).strip()
            if not pid:
                continue

            try:
                await _publish_progress(job_id, {
                    "current": idx + 1, "total": total,
                    "imported": len(imported), "errors": len(errors),
                }, r)
            except AdminOperationAttemptRejected:
                raise
            except Exception:
                pass

            try:
                artist, links = await asyncio.to_thread(
                    danbooru_svc.search_and_extract, pixiv_id=pid)
                if not artist:
                    not_found.append({
                        "pixiv_id": pid,
                        "message": "No matching Danbooru artist found",
                    })
                    continue

                artist_name = artist.get("name", "Unknown")
                artist_id = artist.get("id")

                dl_urls = [
                    u for u in artist.get("urls", [])
                    if u.get("is_active", True) and danbooru_svc.is_downloadable_url(
                        u.get("normalized_url") or u.get("url", ""))
                ]
                if not dl_urls:
                    low_confidence.append({
                        "pixiv_id": pid, "artist_name": artist_name,
                        "artist_id": artist_id,
                        "url_count": len(artist.get("urls", [])),
                        "message": "Found but has no downloadable source URLs",
                    })
                    continue

                applied = await persist_danbooru_artist(artist, links, db)
                await fence_current_admin_operation_transaction(db, task_id=job_id)
                await db.commit()
                # Release all ORM objects from the session so the identity
                # map doesn't grow unboundedly across hundreds of iterations.
                db.expunge_all()

                imported.append({
                    "pixiv_id": pid, "creator_id": str(applied.creator_id),
                    "artist_name": artist_name, "artist_id": artist_id,
                    "links_imported": applied.links_imported,
                    "sources_created": applied.sources_created,
                    "downloadable_urls": [
                        u.get("normalized_url") or u.get("url") for u in dl_urls],
                    "merged": not applied.created_new,
                })

            except AdminOperationAttemptRejected:
                await db.rollback()
                db.expunge_all()
                raise
            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
                # Clean up any objects that may have been loaded before
                # the error, so the session stays lean for the next iteration.
                db.expunge_all()
                errors.append({"pixiv_id": pid, "error": str(e)})
                logger.exception("Batch import failed for pixiv_id=%s", pid)

    result = {
        "total": total,
        "imported_count": len(imported),
        "low_confidence_count": len(low_confidence),
        "not_found_count": len(not_found),
        "error_count": len(errors),
        "imported": imported,
        "low_confidence": low_confidence,
        "not_found": not_found,
        "errors": errors,
    }
    if imported:
        invalidate_creator_subscription_caches()

    try:
        if r is not None:
            r.setex(result_key, RESULT_TTL, json.dumps(result, default=str))
            r.delete(progress_key)
    except Exception:
        pass

    logger.info("Batch import complete: %d imported, %d low, %d not found, %d errors",
                len(imported), len(low_confidence), len(not_found), len(errors))
    return result


async def _url_batch_import(urls: list[str], job_id: str) -> dict:
    """Process a batch of URLs: look up each on Danbooru, auto-import."""
    from app.services import danbooru as danbooru_svc
    from app.services.danbooru_import import persist_danbooru_artist

    imported = []
    not_found = []
    errors = []

    total = len(urls)
    from app.services.operations import (
        AdminOperationAttemptRejected,
        current_admin_operation_attempt,
        fence_current_admin_operation_transaction,
    )

    delivery = current_admin_operation_attempt()
    r = None if delivery is not None else get_redis()
    progress_key, result_key = _redis_keys(job_id)

    async with async_session() as db:
        for idx, url in enumerate(urls):
            url = str(url).strip()
            if not url:
                continue

            try:
                await _publish_progress(job_id, {
                    "current": idx + 1, "total": total,
                    "imported": len(imported), "errors": len(errors),
                }, r)
            except AdminOperationAttemptRejected:
                raise
            except Exception:
                pass

            try:
                artist, links = await asyncio.to_thread(
                    danbooru_svc.search_and_extract,
                    source_url=url,
                    pixiv_id=None,
                    artist_name=None,
                )
                if not artist:
                    not_found.append({
                        "url": url,
                        "message": "No matching Danbooru artist found",
                    })
                    continue

                canonical_name = artist.get("name", "Unknown")
                applied = await persist_danbooru_artist(artist, links, db)
                await fence_current_admin_operation_transaction(db, task_id=job_id)
                await db.commit()
                # Release all ORM objects from the session so the identity
                # map doesn't grow unboundedly across hundreds of iterations.
                db.expunge_all()

                imported.append({
                    "url": url,
                    "creator_id": str(applied.creator_id),
                    "artist_name": canonical_name,
                    "artist_id": artist.get("id"),
                    "links_imported": applied.links_imported,
                    "sources_created": applied.sources_created,
                    "created_new": applied.created_new,
                })

            except AdminOperationAttemptRejected:
                await db.rollback()
                db.expunge_all()
                raise
            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
                # Clean up any objects that may have been loaded before
                # the error, so the session stays lean for the next iteration.
                db.expunge_all()
                errors.append({"url": url, "error": str(e)})
                logger.exception("URL batch import failed for url=%s", url)

    result = {
        "total": total,
        "imported_count": len(imported),
        "not_found_count": len(not_found),
        "error_count": len(errors),
        "imported": imported,
        "not_found": not_found,
        "errors": errors,
    }
    if imported:
        invalidate_creator_subscription_caches()

    try:
        if r is not None:
            r.setex(result_key, RESULT_TTL, json.dumps(result, default=str))
            r.delete(progress_key)
    except Exception:
        pass

    logger.info("URL batch import complete: %d imported, %d not found, %d errors",
                len(imported), len(not_found), len(errors))
    return result
