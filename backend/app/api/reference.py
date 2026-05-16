from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import danbooru as danbooru_svc
from app.models.creator_link import CreatorLink

router = APIRouter(dependencies=[RequireAdmin])


@router.get("/providers")
async def list_reference_providers():
    return {"providers": [{"source_name": "danbooru_reference", "display_name": "Danbooru", "capabilities": {"is_reference_only": True}}]}


@router.post("/danbooru/artist/preview")
async def preview_danbooru_artist(data: dict):
    """Search Danbooru for artists matching a Pixiv URL, artist name, or tag.

    Accepts: {url?: str, pixiv_id?: str, name?: str}
    """
    source_url = data.get("url") or data.get("source_url")
    pixiv_id = data.get("pixiv_id")
    artist_name = data.get("name") or data.get("tag")

    if not source_url and not pixiv_id and not artist_name:
        return {"status": "error", "message": "At least one of url, pixiv_id, or name is required"}

    artist, links = danbooru_svc.search_and_extract(
        source_url=source_url,
        pixiv_id=pixiv_id,
        artist_name=artist_name,
    )

    if not artist:
        return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}

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

    artist, links = danbooru_svc.search_and_extract(
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

    artist, links = danbooru_svc.search_and_extract(
        source_url=source_url, pixiv_id=pixiv_id, artist_name=artist_name,
    )
    if not artist:
        return {"status": "ok", "found": False, "message": "No matching Danbooru artist found"}

    # Step 1: Get or create Creator
    creator_id_str = data.get("creator_id")
    if creator_id_str:
        creator_id = UUID(creator_id_str)
        creator = await db.get(Creator, creator_id)
        if not creator:
            raise HTTPException(status_code=404, detail="Creator not found")
    else:
        display = creator_name or artist.get("name", "Unknown")
        creator = Creator(name=display, display_name=display)
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
        subscription = Subscription(creator_id=creator_id, name=creator_name or None)
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
            is_enabled=True,
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
