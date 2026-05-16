from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import RequireAdmin
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
