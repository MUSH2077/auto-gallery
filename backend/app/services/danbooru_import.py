"""Shared Danbooru one-click import service."""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.creator import Creator
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services import danbooru as danbooru_svc
from app.services.cache import invalidate_creator_subscription_caches
from app.services.creator_dedup import find_existing_creator
from app.services.subscription import get_subscription_defaults


def _extract_source_creator_id(source: str, url: str) -> str | None:
    """Extract the platform-native creator ID from a URL, if possible.

    Returns None when the ID cannot be reliably determined from the URL
    alone (e.g. Twitter numeric ID vs screen-name, or search/pool URLs).
    """
    if source == "pixiv":
        m = re.search(r"pixiv\.net/(?:en/)?users/(\d+)", url)
        return m.group(1) if m else None
    if source == "bilibili":
        m = re.search(r"space\.bilibili\.com/(\d+)", url)
        return m.group(1) if m else None
    return None


def _get_auto_enable_sources() -> set[str]:
    """Read auto-enable-on-import from gallery-dl config.json per extractor."""
    try:
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json"
        )
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            extractors = cfg.get("extractor", {})
            result = set()
            for src_name, src_cfg in extractors.items():
                if src_cfg.get("auto-enable-on-import") is True:
                    result.add("x" if src_name == "twitter" else src_name)
            if result:
                return result
    except Exception:
        pass
    return {"pixiv"}


def _is_source_auto_enabled(source: str) -> bool:
    return source in _get_auto_enable_sources()


async def import_all_danbooru_artist(data: dict, db: AsyncSession) -> dict:
    """Create Creator + Subscription + Sources + Links from Danbooru."""
    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")
    creator_name = data.get("creator_name", "").strip()

    if not source_url and not pixiv_id and not artist_name:
        raise HTTPException(status_code=400, detail="At least one of url, pixiv_id, or name is required")

    artist, links = await asyncio.to_thread(
        danbooru_svc.search_and_extract,
        source_url=source_url,
        pixiv_id=pixiv_id,
        artist_name=artist_name,
    )
    if not artist:
        return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}

    creator_id_str = data.get("creator_id")
    if creator_id_str:
        creator_id = UUID(creator_id_str)
        creator = await db.get(Creator, creator_id)
        if not creator:
            raise HTTPException(status_code=404, detail="Creator not found")
    else:
        canonical_name = artist.get("name", "Unknown")
        chosen_display = creator_name or canonical_name

        pixiv_user_id: str | None = None
        pixiv_profile_url: str | None = None
        for artist_url in artist.get("urls", []):
            raw_url = artist_url.get("normalized_url") or artist_url.get("url", "")
            match = re.search(r"pixiv\.net/(?:en/)?users/(\d+)", raw_url)
            if match:
                pixiv_user_id = match.group(1)
                pixiv_profile_url = f"https://www.pixiv.net/users/{pixiv_user_id}"
                break

        existing = await find_existing_creator(
            db,
            danbooru_artist_id=artist.get("id"),
            source="pixiv" if pixiv_user_id else None,
            source_creator_id=pixiv_user_id,
            source_url=pixiv_profile_url,
        )
        if existing:
            creator = existing
            creator_id = creator.id
            creator_id_str = str(creator_id)
            if creator_name:
                creator.display_name = creator_name
        else:
            creator = Creator(name=canonical_name, display_name=chosen_display)
            db.add(creator)
            await db.flush()
            creator_id = creator.id
            creator_id_str = str(creator_id)

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

    sub_result = await db.execute(select(Subscription).where(Subscription.creator_id == creator_id))
    subscription = sub_result.scalar_one_or_none()
    if not subscription:
        defaults = await get_subscription_defaults(db)
        subscription = Subscription(
            creator_id=creator_id,
            name=creator_name or None,
            sync_interval_hours=defaults["sync_interval_hours"],
            sync_enabled=defaults["sync_enabled"],
            is_active=defaults["is_active"],
        )
        db.add(subscription)
        await db.flush()

    sources_created = 0
    for artist_url in artist.get("urls", []):
        raw_url = artist_url.get("normalized_url") or artist_url.get("url", "")
        if not raw_url or not artist_url.get("is_active", True):
            continue
        if not danbooru_svc.is_downloadable_url(raw_url):
            continue
        existing_ss = await db.execute(
            select(SubscriptionSource).where(
                SubscriptionSource.subscription_id == subscription.id,
                SubscriptionSource.source_url == raw_url,
            )
        )
        if existing_ss.scalar_one_or_none():
            continue
        source = danbooru_svc._classify_url(raw_url)
        sc_id = _extract_source_creator_id(source, raw_url)
        db.add(SubscriptionSource(
            subscription_id=subscription.id,
            source=source,
            source_url=raw_url,
            source_creator_id=sc_id,
            is_enabled=_is_source_auto_enabled(source),
        ))
        sources_created += 1

    danbooru_posts_url = f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(artist['name'])}"
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
            source_creator_id=artist["name"],
            is_enabled=False,
        ))
        sources_created += 1

    # ── Pre-populate SourceCreator records ──────────────────────────
    # The mapping from platform identity → auto-gallery Creator is known
    # at Danbooru import time.  Create SourceCreator rows now so the
    # download importer only needs to look them up, never guess.
    source_creators_created = 0
    seen_sc = set()

    # 1) From downloadable URLs (Pixiv user ID, etc.)
    for artist_url in artist.get("urls", []):
        raw_url = artist_url.get("normalized_url") or artist_url.get("url", "")
        if not raw_url or not artist_url.get("is_active", True):
            continue
        source = danbooru_svc._classify_url(raw_url)
        sc_id = _extract_source_creator_id(source, raw_url)
        if sc_id is None:
            continue
        key = (source, sc_id)
        if key in seen_sc:
            continue
        seen_sc.add(key)
        existing = await db.execute(
            select(SourceCreator).where(
                SourceCreator.source == source,
                SourceCreator.source_creator_id == sc_id,
            )
        )
        if existing.scalar_one_or_none():
            continue
        db.add(SourceCreator(
            source=source,
            source_creator_id=sc_id,
            source_url=raw_url,
            display_name=None,
            creator_id=creator_id,
        ))
        source_creators_created += 1

    # 2) Danbooru artist name → SourceCreator (covers tag-search downloads)
    danbooru_sc_key = ("danbooru", artist["name"])
    if danbooru_sc_key not in seen_sc:
        existing_ds_sc = await db.execute(
            select(SourceCreator).where(
                SourceCreator.source == "danbooru",
                SourceCreator.source_creator_id == artist["name"],
            )
        )
        if not existing_ds_sc.scalar_one_or_none():
            db.add(SourceCreator(
                source="danbooru",
                source_creator_id=artist["name"],
                source_url=danbooru_posts_url,
                display_name=None,
                creator_id=creator_id,
            ))
            source_creators_created += 1

    await db.commit()
    invalidate_creator_subscription_caches()

    return {
        "status": "ok",
        "found": True,
        "creator_id": creator_id_str,
        "artist_name": artist["name"],
        "links_imported": links_created,
        "sources_created": sources_created,
        "source_creators_created": source_creators_created,
        "subscription_id": str(subscription.id),
    }
