"""RQ job: batch import Pixiv user IDs and URLs via Danbooru one-click flow.

Runs asynchronously in the RQ worker so it can handle large batches without
HTTP request timeouts. Redis keys are scoped by job_id to support concurrent imports.
"""

import asyncio
import json
import logging
import os
import urllib.parse
from app.config import settings
from app.services.redis_client import get_redis
from app.database import async_session
from app.services.cache import invalidate_creator_subscription_caches

logger = logging.getLogger(__name__)

PROGRESS_TTL = 3600   # 1 hour for in-progress data (matches RQ job_timeout)
RESULT_TTL = 7200     # 2 hours for completed results


def _redis_keys(job_id: str) -> tuple[str, str]:
    """Return (progress_key, result_key) scoped to this job."""
    return (f"batch_import:{job_id}:progress", f"batch_import:{job_id}:result")


def _get_auto_enable_sources() -> set[str]:
    """Read auto-enable-on-import from gallery-dl config.json per extractor."""
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
                    if src_name == "twitter":
                        result.add("x")
                    else:
                        result.add(src_name)
            if result:
                return result
    except Exception:
        pass
    return {"pixiv"}


def run_batch_import(pixiv_ids: list[str], job_id: str = ""):
    """Entry point for RQ worker — Pixiv ID batch import."""
    return asyncio.run(_batch_import(pixiv_ids, job_id))


def run_url_batch_import(urls: list[str], job_id: str = ""):
    """Entry point for RQ worker — URL batch import."""
    return asyncio.run(_url_batch_import(urls, job_id))


async def _batch_import(pixiv_ids: list[str], job_id: str) -> dict:
    """Process a batch of Pixiv user IDs: search Danbooru, auto-import."""
    from app.models.creator import Creator
    from app.models.creator_link import CreatorLink
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services import danbooru as danbooru_svc
    from app.services.creator_dedup import find_existing_creator
    from sqlalchemy import select

    imported = []
    low_confidence = []
    not_found = []
    errors = []

    total = len(pixiv_ids)
    r = get_redis()
    progress_key, result_key = _redis_keys(job_id)

    async with async_session() as db:
        for idx, pid in enumerate(pixiv_ids):
            pid = str(pid).strip()
            if not pid:
                continue

            try:
                r.setex(progress_key, PROGRESS_TTL, json.dumps({
                    "current": idx + 1, "total": total,
                    "imported": len(imported), "errors": len(errors),
                }))
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

                first_dl = dl_urls[0] if dl_urls else {}
                first_url = first_dl.get("normalized_url") or first_dl.get("url", "")
                existing = await find_existing_creator(
                    db, danbooru_artist_id=artist_id,
                    source="pixiv", source_creator_id=pid,
                    source_url=first_url,
                )

                if existing:
                    creator = existing
                    was_merged = True
                    if not creator.danbooru_artist_id:
                        creator.danbooru_artist_id = artist_id
                else:
                    from app.models.source_creator import SourceCreator
                    sc_result = await db.execute(
                        select(SourceCreator).where(
                            SourceCreator.source == "pixiv",
                            SourceCreator.source_creator_id == str(pid),
                        )
                    )
                    sc = sc_result.scalar_one_or_none()
                    pixiv_display = sc.display_name if sc else None
                    creator = Creator(
                        name=artist_name,
                        display_name=pixiv_display or artist_name,
                        danbooru_artist_id=artist_id,
                    )
                    db.add(creator)
                    await db.flush()
                    was_merged = False

                    parts = []
                    other_names = artist.get("other_names", [])
                    if other_names:
                        parts.append(f"Danbooru aliases: {', '.join(other_names)}")
                    notes = artist.get("notes")
                    if notes:
                        parts.append(notes)
                    if parts:
                        creator.description = "\n".join(parts)

                links_count = 0
                for link_data in links:
                    existing_link = await db.execute(
                        select(CreatorLink).where(
                            CreatorLink.creator_id == creator.id,
                            CreatorLink.url == link_data["url"]))
                    if existing_link.scalar_one_or_none():
                        continue
                    db.add(CreatorLink(
                        creator_id=creator.id, url=link_data["url"],
                        link_type=link_data["link_type"],
                        source=link_data["source"],
                        confidence=link_data["confidence"],
                        is_verified=link_data["is_verified"],
                        notes=link_data.get("notes"),
                    ))
                    links_count += 1

                sub_result = await db.execute(
                    select(Subscription).where(Subscription.creator_id == creator.id))
                subscription = sub_result.scalar_one_or_none()
                if not subscription:
                    from app.services.subscription import get_subscription_defaults
                    defaults = await get_subscription_defaults(db)
                    subscription = Subscription(creator_id=creator.id,
                        sync_interval_hours=defaults["sync_interval_hours"],
                        sync_enabled=defaults["sync_enabled"],
                        is_active=defaults["is_active"])
                    db.add(subscription)
                    await db.flush()

                sources_count = 0
                seen_source_types: set[str] = set()
                for u in dl_urls:
                    raw_url = u.get("normalized_url") or u.get("url", "")
                    src_type = danbooru_svc._classify_url(raw_url)
                    # Dedup by source type within this batch (a single artist
                    # may have multiple URLs of the same platform, e.g. two
                    # weibo URLs, but the DB constraint is (subscription_id,
                    # source) — only one per source type per subscription).
                    if src_type in seen_source_types:
                        continue
                    seen_source_types.add(src_type)
                    # Also check if this source type already exists in DB
                    existing_ss = await db.execute(
                        select(SubscriptionSource).where(
                            SubscriptionSource.subscription_id == subscription.id,
                            SubscriptionSource.source == src_type))
                    if existing_ss.scalar_one_or_none():
                        continue
                    db.add(SubscriptionSource(
                        subscription_id=subscription.id,
                        source=src_type, source_url=raw_url,
                        is_enabled=(src_type in _get_auto_enable_sources()),
                    ))
                    sources_count += 1

                danbooru_posts_url = (
                    f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(artist_name)}"
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
                        is_enabled=("danbooru" in _get_auto_enable_sources()),
                    ))
                    sources_count += 1

                await db.commit()

                imported.append({
                    "pixiv_id": pid, "creator_id": str(creator.id),
                    "artist_name": artist_name, "artist_id": artist_id,
                    "links_imported": links_count,
                    "sources_created": sources_count,
                    "downloadable_urls": [
                        u.get("normalized_url") or u.get("url") for u in dl_urls],
                    "merged": was_merged,
                })

            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
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
        r.setex(result_key, RESULT_TTL, json.dumps(result, default=str))
        r.delete(progress_key)
    except Exception:
        pass

    logger.info("Batch import complete: %d imported, %d low, %d not found, %d errors",
                len(imported), len(low_confidence), len(not_found), len(errors))
    return result


async def _url_batch_import(urls: list[str], job_id: str) -> dict:
    """Process a batch of URLs: look up each on Danbooru, auto-import."""
    from app.models.creator import Creator
    from app.models.creator_link import CreatorLink
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services import danbooru as danbooru_svc
    from app.services.creator_dedup import find_existing_creator
    from sqlalchemy import select

    imported = []
    not_found = []
    errors = []

    total = len(urls)
    r = get_redis()
    progress_key, result_key = _redis_keys(job_id)

    async with async_session() as db:
        for idx, url in enumerate(urls):
            url = str(url).strip()
            if not url:
                continue

            try:
                r.setex(progress_key, PROGRESS_TTL, json.dumps({
                    "current": idx + 1, "total": total,
                    "imported": len(imported), "errors": len(errors),
                }))
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
                existing = await find_existing_creator(
                    db, danbooru_artist_id=artist.get("id"))
                if existing:
                    creator = existing
                    created_new = False
                else:
                    creator = Creator(
                        name=canonical_name,
                        display_name=canonical_name,
                    )
                    db.add(creator)
                    await db.flush()
                    created_new = True

                if creator.danbooru_artist_id is None:
                    creator.danbooru_artist_id = artist.get("id")

                links_created = 0
                for link_data in links:
                    existing_link = await db.execute(
                        select(CreatorLink).where(
                            CreatorLink.creator_id == creator.id,
                            CreatorLink.url == link_data["url"],
                        )
                    )
                    if existing_link.scalar_one_or_none():
                        continue
                    db.add(CreatorLink(
                        creator_id=creator.id,
                        url=link_data["url"],
                        link_type=link_data["link_type"],
                        source=link_data["source"],
                        confidence=link_data["confidence"],
                        is_verified=link_data["is_verified"],
                        notes=link_data.get("notes"),
                    ))
                    links_created += 1

                sub_result = await db.execute(
                    select(Subscription).where(
                        Subscription.creator_id == creator.id))
                subscription = sub_result.scalar_one_or_none()
                if not subscription:
                    from app.services.subscription import get_subscription_defaults
                    defaults = await get_subscription_defaults(db)
                    subscription = Subscription(
                        creator_id=creator.id,
                        sync_interval_hours=defaults["sync_interval_hours"],
                        sync_enabled=defaults["sync_enabled"],
                        is_active=defaults["is_active"],
                    )
                    db.add(subscription)
                    await db.flush()

                sources_created = 0
                for link_data in links:
                    if not danbooru_svc.is_downloadable_url(link_data["url"]):
                        continue
                    src_type = danbooru_svc._classify_url(link_data["url"])
                    existing_ss = await db.execute(
                        select(SubscriptionSource).where(
                            SubscriptionSource.subscription_id == subscription.id,
                            SubscriptionSource.source_url == link_data["url"],
                        )
                    )
                    if existing_ss.scalar_one_or_none():
                        continue
                    db.add(SubscriptionSource(
                        subscription_id=subscription.id,
                        source=src_type,
                        source_url=link_data["url"],
                        is_enabled=(src_type in _get_auto_enable_sources()),
                    ))
                    sources_created += 1

                danbooru_posts_url = (
                    f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(canonical_name)}"
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
                        is_enabled=("danbooru" in _get_auto_enable_sources()),
                    ))
                    sources_created += 1

                await db.commit()

                imported.append({
                    "url": url,
                    "creator_id": str(creator.id),
                    "artist_name": canonical_name,
                    "artist_id": artist.get("id"),
                    "links_imported": links_created,
                    "sources_created": sources_created,
                    "created_new": created_new,
                })

            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
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
        r.setex(result_key, RESULT_TTL, json.dumps(result, default=str))
        r.delete(progress_key)
    except Exception:
        pass

    logger.info("URL batch import complete: %d imported, %d not found, %d errors",
                len(imported), len(not_found), len(errors))
    return result
