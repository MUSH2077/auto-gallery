"""RQ job: batch import Pixiv user IDs via Danbooru one-click flow.

Runs asynchronously in the RQ worker so it can handle large batches without
HTTP request timeouts.
"""

import asyncio
import json
import logging
import urllib.parse
import redis as redis_lib
from uuid import UUID

from app.config import settings
from app.database import async_session

logger = logging.getLogger(__name__)

RESULT_TTL = 3600  # 1 hour


def run_batch_import(pixiv_ids: list[str]):
    """Entry point for RQ worker (sync wrapper around async main)."""
    return asyncio.run(_batch_import(pixiv_ids))


async def _batch_import(pixiv_ids: list[str]) -> dict:
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
    r = redis_lib.from_url(settings.redis_url)

    async with async_session() as db:
        for idx, pid in enumerate(pixiv_ids):
            pid = str(pid).strip()
            if not pid:
                continue

            # Update progress in Redis
            try:
                r.setex("batch_import:progress", 300, json.dumps({
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
                    creator = Creator(
                        name=artist_name, display_name=artist_name,
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
                    subscription = Subscription(creator_id=creator.id)
                    db.add(subscription)
                    await db.flush()

                sources_count = 0
                for u in dl_urls:
                    raw_url = u.get("normalized_url") or u.get("url", "")
                    src_type = danbooru_svc._classify_url(raw_url)
                    existing_ss = await db.execute(
                        select(SubscriptionSource).where(
                            SubscriptionSource.subscription_id == subscription.id,
                            SubscriptionSource.source_url == raw_url))
                    if existing_ss.scalar_one_or_none():
                        continue
                    db.add(SubscriptionSource(
                        subscription_id=subscription.id,
                        source=src_type, source_url=raw_url,
                        is_enabled=(src_type == "pixiv"),
                    ))
                    sources_count += 1

                # Also create Danbooru subscription source (default disabled)
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
                        is_enabled=False,
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

    # Store final result in Redis
    try:
        r.setex("batch_import:result", RESULT_TTL, json.dumps(result, default=str))
        r.delete("batch_import:progress")
    except Exception:
        pass

    logger.info("Batch import complete: %d imported, %d low, %d not found, %d errors",
                len(imported), len(low_confidence), len(not_found), len(errors))
    return result
