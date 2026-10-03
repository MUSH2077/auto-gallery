"""Shared persistence rules for resolved Danbooru artists; callers own commits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import re
import urllib.parse
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import case, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.creator import Creator
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services import danbooru as danbooru_svc
from app.services.cache import invalidate_creator_subscription_caches
from app.services.subscription import get_subscription_defaults


def _extract_source_creator_id(source: str, url: str) -> str | None:
    """Only use native identities that can be resolved from a profile URL."""
    if source == "pixiv":
        match = re.search(r"pixiv\.net/(?:en/)?users/(\d+)", url)
        return match.group(1) if match else None
    if source == "bilibili":
        match = re.search(r"space\.bilibili\.com/(\d+)", url)
        return match.group(1) if match else None
    return None


def _get_auto_enable_sources() -> set[str]:
    """Read the existing gallery-dl import policy, with its Pixiv default."""
    try:
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json"
        )
        if os.path.exists(config_path):
            with open(config_path) as file:
                cfg = json.load(file)
            result = {
                "x" if name == "twitter" else name
                for name, options in cfg.get("extractor", {}).items()
                if options.get("auto-enable-on-import") is True
            }
            if result:
                return result
    except Exception:
        pass
    return {"pixiv"}


@dataclass(frozen=True)
class AppliedArtistImport:
    creator_id: UUID
    subscription_id: UUID
    created_new: bool
    links_imported: int
    sources_created: int
    source_creators_created: int


async def _match_artist_creator(db, *, artist_id, identities, urls):
    """Resolve all valid markers together; conflicting owners are not merged."""
    matches = [Creator.id.in_(select(SourceCreator.creator_id).where(
        tuple_(SourceCreator.source, SourceCreator.source_creator_id).in_(list(identities)),
    ))]
    if artist_id is not None:
        matches.append(Creator.danbooru_artist_id == artist_id)
        urls = {*urls, f"https://danbooru.donmai.us/artists/{artist_id}"}
    if urls:
        matches.append(Creator.id.in_(select(CreatorLink.creator_id).where(CreatorLink.url.in_(urls))))
    creators = list((await db.execute(select(Creator).where(or_(*matches)).order_by(Creator.id))).scalars())
    if len(creators) > 1:
        raise HTTPException(status_code=409, detail={
            "code": "danbooru_creator_conflict",
            "message": "Danbooru artist matches multiple local creators; choose an explicit target",
            "creator_ids": [str(creator.id) for creator in creators],
        })
    return creators[0] if creators else None


async def persist_danbooru_artist(
    artist: dict, links: list[dict], db: AsyncSession, *,
    creator_id: UUID | None = None, creator_name: str = "",
) -> AppliedArtistImport:
    """Apply resolved identity, sources and projections without committing.

    New display names prefer explicit input, then known Pixiv, Bilibili and
    Danbooru identity names (stable native-ID order), then the Danbooru tag.
    Existing display names only change on explicit input.
    Existing enablement and nonempty identity ownership are preserved.
    """
    canonical_name = artist.get("name", "Unknown")
    active_urls = {
        url: danbooru_svc._classify_url(url)
        for entry in artist.get("urls", [])
        if entry.get("is_active", True)
        and (url := entry.get("normalized_url") or entry.get("url", ""))
    }
    identities = {}
    for url in sorted(active_urls):
        source = active_urls[url]
        if (native_id := _extract_source_creator_id(source, url)) is not None:
            identities.setdefault((source, native_id), url)
    posts_url = f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(canonical_name)}"
    identities[("danbooru", canonical_name)] = posts_url
    if creator_id is not None:
        creator = await db.get(Creator, creator_id)
        if creator is None:
            raise HTTPException(status_code=404, detail="Creator not found")
    else:
        creator = await _match_artist_creator(db, artist_id=artist.get("id"), identities=identities, urls=set(active_urls))
    created_new = creator is None
    if created_new:
        native = (await db.execute(select(SourceCreator).where(
            tuple_(SourceCreator.source, SourceCreator.source_creator_id).in_(list(identities)),
            SourceCreator.display_name.is_not(None), SourceCreator.display_name != "",
        ).order_by(
            case((SourceCreator.source == "pixiv", 0), (SourceCreator.source == "bilibili", 1), else_=2),
            SourceCreator.source, SourceCreator.source_creator_id,
        ).limit(1))).scalar_one_or_none()
        creator = Creator(name=canonical_name,
                          display_name=creator_name or (native.display_name if native else None) or canonical_name)
        db.add(creator)
        await db.flush()
    elif creator_name:
        creator.display_name = creator_name
    if creator.danbooru_artist_id is None:
        creator.danbooru_artist_id = artist.get("id")

    links_created = 0
    seen_links = set()
    for link in links:
        if link["url"] in seen_links:
            continue
        seen_links.add(link["url"])
        existing = (await db.execute(select(CreatorLink).where(
            CreatorLink.creator_id == creator.id, CreatorLink.url == link["url"],
        ))).scalar_one_or_none()
        if existing is None:
            db.add(CreatorLink(creator_id=creator.id, url=link["url"], link_type=link["link_type"],
                               source=link["source"], confidence=link["confidence"],
                               is_verified=link["is_verified"], notes=link.get("notes")))
            links_created += 1

    subscription = (await db.execute(select(Subscription).where(
        Subscription.creator_id == creator.id,
    ))).scalar_one_or_none()
    if subscription is None:
        defaults = await get_subscription_defaults(db)
        subscription = Subscription(creator_id=creator.id, name=creator_name or None,
                                    sync_interval_hours=defaults["sync_interval_hours"],
                                    sync_enabled=defaults["sync_enabled"], is_active=defaults["is_active"])
        db.add(subscription)
        await db.flush()

    sources = {url: source for url, source in active_urls.items() if danbooru_svc.is_downloadable_url(url)}
    sources[posts_url] = "danbooru"
    enabled_sources = _get_auto_enable_sources()
    sources_created = 0
    for url, source in sources.items():
        native_id = canonical_name if url == posts_url else _extract_source_creator_id(source, url)
        existing = (await db.execute(select(SubscriptionSource).where(
            SubscriptionSource.subscription_id == subscription.id, SubscriptionSource.source_url == url,
        ))).scalar_one_or_none()
        if existing is None:
            db.add(SubscriptionSource(subscription_id=subscription.id, source=source, source_url=url,
                                      source_creator_id=native_id, is_enabled=source in enabled_sources))
            sources_created += 1
        elif existing.source_creator_id is None and native_id is not None:
            existing.source_creator_id = native_id

    source_creators_created = 0
    for (source, native_id), url in identities.items():
        existing = (await db.execute(select(SourceCreator).where(
            SourceCreator.source == source, SourceCreator.source_creator_id == native_id,
        ))).scalar_one_or_none()
        if existing is None:
            db.add(SourceCreator(source=source, source_creator_id=native_id,
                                 source_url=url, display_name=None, creator_id=creator.id))
            source_creators_created += 1
        elif existing.creator_id is None:
            existing.creator_id = creator.id

    from app.services.creator import CreatorService
    from app.services.creator_aliases import backfill_creator_alias_batch
    await backfill_creator_alias_batch(db, (creator.id,), request_projection=False)
    await CreatorService(db)._request_creator_projection(creator.id)
    await db.flush()
    return AppliedArtistImport(creator.id, subscription.id, created_new, links_created,
                               sources_created, source_creators_created)


async def import_all_danbooru_artist(data: dict, db: AsyncSession) -> dict:
    """Resolve one artist, apply shared rules, then commit and invalidate."""
    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")
    if not source_url and not pixiv_id and not artist_name:
        raise HTTPException(status_code=400, detail="At least one of url, pixiv_id, or name is required")
    artist, links = await asyncio.to_thread(
        danbooru_svc.search_and_extract, source_url=source_url,
        pixiv_id=pixiv_id, artist_name=artist_name,
    )
    if not artist:
        return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}
    applied = await persist_danbooru_artist(
        artist, links, db, creator_id=UUID(str(data["creator_id"])) if data.get("creator_id") else None,
        creator_name=data.get("creator_name", "").strip(),
    )
    from app.services.operations import fence_current_admin_operation_transaction
    await fence_current_admin_operation_transaction(db)
    await db.commit()
    invalidate_creator_subscription_caches()
    return {
        "status": "ok", "found": True, "creator_id": str(applied.creator_id),
        "artist_name": artist["name"], "links_imported": applied.links_imported,
        "sources_created": applied.sources_created, "source_creators_created": applied.source_creators_created,
        "subscription_id": str(applied.subscription_id),
    }
