"""Creator identity provisioning for disk imports.

Disk import should recover a normal creator/subscription/source chain before it
creates a synthetic recovery DownloadJob.  Danbooru is used as enrichment when
available, but local metadata remains enough to continue safely.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.creator import Creator
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services import danbooru as danbooru_svc
from app.services.creator_dedup import find_existing_creator
from app.services.danbooru_import import _extract_source_creator_id


@dataclass(slots=True)
class MetadataIdentity:
    source: str
    source_creator_id: str
    source_url: str
    display_name: str
    raw_metadata: dict[str, Any]
    creator_dir: str


@dataclass(slots=True)
class ProvisionedIdentity:
    creator: Creator
    subscription: Subscription
    subscription_source: SubscriptionSource
    source_creator: SourceCreator
    enrichment_status: str
    created_creator: bool = False
    created_subscription: bool = False
    created_source: bool = False


def _slug(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_\-]+", "_", value.strip()).strip("_").lower()
    return text[:180] or fallback


def _profile_url(source: str, source_creator_id: str, fallback: str | None = None) -> str:
    if fallback:
        return fallback
    if source == "pixiv":
        return f"https://www.pixiv.net/users/{source_creator_id}"
    if source == "bilibili":
        return f"https://space.bilibili.com/{source_creator_id}"
    if source == "iwara":
        return f"https://www.iwara.tv/profile/{source_creator_id}"
    if source == "x":
        return f"https://x.com/{source_creator_id}"
    if source == "lofter":
        return f"https://{source_creator_id}.lofter.com/"
    if source == "pinterest":
        return f"https://www.pinterest.com/{source_creator_id}/"
    if source == "danbooru":
        return f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(source_creator_id)}"
    return f"recovery://{source}/{source_creator_id}"


def extract_metadata_identity(source: str, raw_metadata: dict[str, Any], creator_dir: str) -> MetadataIdentity:
    provider = registry.get(source)
    parsed = provider.parse_source_creator(raw_metadata)
    source_creator_id = str(parsed.get("source_creator_id") or creator_dir or "unknown").strip()
    display_name = str(parsed.get("display_name") or source_creator_id).strip()
    source_url = _profile_url(source, source_creator_id, parsed.get("source_url"))
    return MetadataIdentity(
        source=source,
        source_creator_id=source_creator_id,
        source_url=source_url,
        display_name=display_name,
        raw_metadata=parsed.get("raw_metadata") or raw_metadata,
        creator_dir=creator_dir,
    )


async def _ensure_subscription(db: AsyncSession, creator: Creator, identity: MetadataIdentity) -> tuple[Subscription, bool]:
    existing = (await db.execute(select(Subscription).where(Subscription.creator_id == creator.id))).scalar_one_or_none()
    if existing:
        return existing, False
    sub = Subscription(
        creator_id=creator.id,
        name=identity.display_name,
        is_active=True,
        sync_enabled=False,
        sync_interval_hours=24,
        schedule_mode="manual",
    )
    db.add(sub)
    await db.flush()
    return sub, True


async def _find_creator_from_subscription_source(db: AsyncSession, identity: MetadataIdentity) -> Creator | None:
    result = await db.execute(
        select(Creator)
        .join(Subscription, Subscription.creator_id == Creator.id)
        .join(SubscriptionSource, SubscriptionSource.subscription_id == Subscription.id)
        .where(
            SubscriptionSource.source == identity.source,
            (
                (SubscriptionSource.source_creator_id == identity.source_creator_id)
                | (SubscriptionSource.source_url == identity.source_url)
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _ensure_subscription_source(
    db: AsyncSession,
    subscription: Subscription,
    identity: MetadataIdentity,
) -> tuple[SubscriptionSource, bool]:
    existing = (await db.execute(
        select(SubscriptionSource).where(
            SubscriptionSource.subscription_id == subscription.id,
            or_(
                SubscriptionSource.source_url == identity.source_url,
                SubscriptionSource.source == identity.source,
            ),
        )
    )).scalar_one_or_none()
    if existing:
        existing.source_creator_id = existing.source_creator_id or identity.source_creator_id
        existing.source_url = existing.source_url or identity.source_url
        return existing, False
    ss = SubscriptionSource(
        subscription_id=subscription.id,
        source=identity.source,
        source_creator_id=identity.source_creator_id,
        source_url=identity.source_url,
        is_enabled=False,
        auth_healthy=True,
    )
    db.add(ss)
    await db.flush()
    return ss, True


async def _ensure_source_creator(
    db: AsyncSession,
    creator: Creator,
    identity: MetadataIdentity,
    *,
    needs_enrichment: bool,
    enrichment_status: str,
) -> SourceCreator:
    existing = (await db.execute(
        select(SourceCreator).where(
            SourceCreator.source == identity.source,
            SourceCreator.source_creator_id == identity.source_creator_id,
        )
    )).scalar_one_or_none()
    raw = {
        **(identity.raw_metadata if isinstance(identity.raw_metadata, dict) else {}),
        "_disk_import": {
            "needs_enrichment": needs_enrichment,
            "enrichment_status": enrichment_status,
        },
    }
    if existing:
        if existing.creator_id is None:
            existing.creator_id = creator.id
        existing.source_url = existing.source_url or identity.source_url
        existing.display_name = existing.display_name or identity.display_name
        existing.raw_metadata = existing.raw_metadata or raw
        return existing
    sc = SourceCreator(
        creator_id=creator.id,
        source=identity.source,
        source_creator_id=identity.source_creator_id,
        source_url=identity.source_url,
        display_name=identity.display_name,
        raw_metadata=raw,
    )
    db.add(sc)
    await db.flush()
    return sc


async def _apply_danbooru_artist(
    db: AsyncSession,
    identity: MetadataIdentity,
    artist: dict[str, Any],
    links: list[dict[str, Any]],
    creator: Creator | None = None,
) -> Creator:
    # An explicitly passed creator wins — re-enrichment must wire the mapping
    # onto THAT creator, never onto whatever identity resolution turns up.
    existing = creator
    if existing is None:
        existing = await find_existing_creator(
            db,
            danbooru_artist_id=artist.get("id"),
            source=identity.source,
            source_creator_id=identity.source_creator_id,
            source_url=identity.source_url,
        ) or await _find_creator_from_subscription_source(db, identity)
    if existing:
        creator = existing
        if identity.display_name and not creator.display_name:
            creator.display_name = identity.display_name
    else:
        creator = Creator(name=artist.get("name") or _slug(identity.display_name, f"{identity.source}_{identity.source_creator_id}"),
                          display_name=identity.display_name or artist.get("name"))
        db.add(creator)
        await db.flush()

    if creator.danbooru_artist_id is None:
        creator.danbooru_artist_id = artist.get("id")
    # description is left for the admin to write — auto-generated text made
    # creator pages differ by import path (aliases blurb vs disk-import blurb).

    for link_data in links:
        if not link_data.get("url"):
            continue
        exists = (await db.execute(select(CreatorLink).where(
            CreatorLink.creator_id == creator.id,
            CreatorLink.url == link_data["url"],
        ))).scalar_one_or_none()
        if exists:
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

    subscription, _ = await _ensure_subscription(db, creator, identity)
    for artist_url in artist.get("urls", []):
        raw_url = artist_url.get("normalized_url") or artist_url.get("url", "")
        if not raw_url or not artist_url.get("is_active", True) or not danbooru_svc.is_downloadable_url(raw_url):
            continue
        source = danbooru_svc._classify_url(raw_url)
        sc_id = _extract_source_creator_id(source, raw_url)
        existing_ss = (await db.execute(select(SubscriptionSource).where(
            SubscriptionSource.subscription_id == subscription.id,
            or_(
                SubscriptionSource.source_url == raw_url,
                SubscriptionSource.source == source,
            ),
        ))).scalar_one_or_none()
        if existing_ss:
            existing_ss.source_url = existing_ss.source_url or raw_url
            existing_ss.source_creator_id = existing_ss.source_creator_id or sc_id
        else:
            db.add(SubscriptionSource(
                subscription_id=subscription.id,
                source=source,
                source_url=raw_url,
                source_creator_id=sc_id,
                is_enabled=False,
            ))
        if sc_id:
            existing_sc = (await db.execute(select(SourceCreator).where(
                SourceCreator.source == source,
                SourceCreator.source_creator_id == sc_id,
            ))).scalar_one_or_none()
            if not existing_sc:
                db.add(SourceCreator(
                    creator_id=creator.id,
                    source=source,
                    source_creator_id=sc_id,
                    source_url=raw_url,
                ))

    danbooru_url = f"https://danbooru.donmai.us/posts?tags={urllib.parse.quote(artist['name'])}"
    if not (await db.execute(select(SubscriptionSource).where(
        SubscriptionSource.subscription_id == subscription.id,
        SubscriptionSource.source == "danbooru",
    ))).scalar_one_or_none():
        db.add(SubscriptionSource(
            subscription_id=subscription.id,
            source="danbooru",
            source_url=danbooru_url,
            source_creator_id=artist["name"],
            is_enabled=False,
        ))
    return creator


async def _try_danbooru_enrichment(identity: MetadataIdentity) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    pixiv_id = identity.source_creator_id if identity.source == "pixiv" and identity.source_creator_id != "unknown" else None
    artist_name = identity.source_creator_id if identity.source == "danbooru" else None
    try:
        artist, links = await asyncio.to_thread(
            danbooru_svc.search_and_extract,
            source_url=identity.source_url,
            pixiv_id=pixiv_id,
            artist_name=artist_name,
        )
    except Exception as exc:  # Danbooru is enrichment, not a hard dependency.
        return None, [], f"danbooru_error:{type(exc).__name__}"
    if not artist:
        return None, [], "danbooru_not_found"
    return artist, links, "danbooru_found"


async def provision_identity_for_disk_import(
    db: AsyncSession,
    identity: MetadataIdentity,
    *,
    prefer_danbooru: bool = True,
) -> ProvisionedIdentity:
    artist = None
    links: list[dict[str, Any]] = []
    enrichment_status = "metadata_only"
    if prefer_danbooru:
        artist, links, enrichment_status = await _try_danbooru_enrichment(identity)

    if artist:
        created_creator = False
        creator = await _apply_danbooru_artist(db, identity, artist, links)
    else:
        existing = await find_existing_creator(
            db,
            source=identity.source,
            source_creator_id=identity.source_creator_id,
            source_url=identity.source_url,
        ) or await _find_creator_from_subscription_source(db, identity)
        if existing:
            created_creator = False
            creator = existing
            if not creator.display_name:
                creator.display_name = identity.display_name
        else:
            created_creator = True
            name = _slug(identity.display_name or identity.source_creator_id, f"{identity.source}_{identity.source_creator_id}")
            # No auto description — provenance and enrichment status live in
            # the source_creator's _disk_import marker, not in user-facing text.
            creator = Creator(
                name=name,
                display_name=identity.display_name,
            )
            db.add(creator)
            await db.flush()

    subscription, created_subscription = await _ensure_subscription(db, creator, identity)
    subscription_source, created_source = await _ensure_subscription_source(db, subscription, identity)
    source_creator = await _ensure_source_creator(
        db,
        creator,
        identity,
        needs_enrichment=not bool(artist),
        enrichment_status=enrichment_status,
    )
    return ProvisionedIdentity(
        creator=creator,
        subscription=subscription,
        subscription_source=subscription_source,
        source_creator=source_creator,
        enrichment_status=enrichment_status,
        created_creator=created_creator,
        created_subscription=created_subscription,
        created_source=created_source,
    )
