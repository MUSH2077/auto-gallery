"""Danbooru-first creator enrichment — shared by creation paths and recovery.

Every path that creates a creator should try to establish the Danbooru
identity mapping. Danbooru stays enrichment, not a hard dependency (see
.claude/constraints/danbooru-reference.md): a miss or an outage never blocks
creation — the outcome is recorded on the source_creator so it can be
retried later via reenrich_pending().
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.creator import Creator
from app.models.source_creator import SourceCreator
from app.services import danbooru as danbooru_svc
from app.services.danbooru import DanbooruUnavailableError
from app.services.disk_identity import (
    MetadataIdentity,
    _apply_danbooru_artist,
    _profile_url,
)

logger = logging.getLogger(__name__)

STATUS_FOUND = "danbooru_found"
STATUS_NOT_FOUND = "danbooru_not_found"


def _identity_from_source_creator(sc: SourceCreator) -> MetadataIdentity:
    return MetadataIdentity(
        source=sc.source,
        source_creator_id=sc.source_creator_id,
        source_url=sc.source_url or _profile_url(sc.source, sc.source_creator_id),
        display_name=sc.display_name or sc.source_creator_id,
        raw_metadata=sc.raw_metadata if isinstance(sc.raw_metadata, dict) else {},
        creator_dir="",
    )


def _mark_attempt(sc: SourceCreator, status: str) -> None:
    raw = sc.raw_metadata if isinstance(sc.raw_metadata, dict) else {}
    marker = {
        **(raw.get("_disk_import") or {}),
        "needs_enrichment": status != STATUS_FOUND,
        "enrichment_status": status,
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
    }
    # Reassign instead of mutating: SQLAlchemy does not track in-place JSONB edits.
    sc.raw_metadata = {**raw, "_disk_import": marker}


async def enrich_creator_from_danbooru(
    db: AsyncSession,
    creator: Creator,
    source_creator: SourceCreator | None = None,
) -> dict[str, Any]:
    """Search Danbooru for this creator and wire the mapping onto it.

    Search hints come from the source_creator when given (URL / pixiv id),
    otherwise from the creator's display name. Returns a status dict and
    never raises for misses or outages; the caller owns the commit.
    """
    if source_creator is not None:
        identity = _identity_from_source_creator(source_creator)
        pixiv_id = (
            identity.source_creator_id
            if identity.source == "pixiv" and identity.source_creator_id != "unknown"
            else None
        )
        artist_name = identity.source_creator_id if identity.source == "danbooru" else None
        source_url = identity.source_url
    else:
        identity = MetadataIdentity(
            source="manual",
            source_creator_id=creator.name,
            source_url="",
            display_name=creator.display_name or creator.name,
            raw_metadata={},
            creator_dir="",
        )
        pixiv_id = None
        artist_name = creator.display_name or creator.name
        source_url = None

    try:
        artist, links = await asyncio.to_thread(
            danbooru_svc.search_and_extract,
            source_url=source_url or None,
            pixiv_id=pixiv_id,
            artist_name=artist_name,
        )
    except DanbooruUnavailableError as exc:
        logger.warning("Danbooru unavailable while enriching creator %s: %s", creator.id, exc)
        status = f"danbooru_error:{type(exc).__name__}"
        if source_creator is not None:
            _mark_attempt(source_creator, status)
        return {"status": status}

    if not artist:
        if source_creator is not None:
            _mark_attempt(source_creator, STATUS_NOT_FOUND)
        return {"status": STATUS_NOT_FOUND}

    await _apply_danbooru_artist(db, identity, artist, links, creator=creator)
    if source_creator is not None:
        _mark_attempt(source_creator, STATUS_FOUND)
    return {
        "status": STATUS_FOUND,
        "artist_id": artist.get("id"),
        "artist_name": artist.get("name"),
    }


async def enrich_creator_by_id(db: AsyncSession, creator_id: UUID) -> dict[str, Any]:
    """Enrich one creator (API entry point). Commits on completion."""
    creator = await db.get(Creator, creator_id)
    if creator is None:
        raise ValueError("Creator not found")

    scs = (
        (await db.execute(
            select(SourceCreator)
            .where(SourceCreator.creator_id == creator_id)
            .order_by(SourceCreator.created_at)
        )).scalars().all()
    )
    # Pixiv profiles are what Danbooru indexes best; prefer them as the hint.
    sc = next((s for s in scs if s.source == "pixiv"), scs[0] if scs else None)

    result = await enrich_creator_from_danbooru(db, creator, source_creator=sc)
    await db.commit()
    return result


async def reenrich_pending(
    db: AsyncSession,
    *,
    limit: int = 500,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Retry the Danbooru mapping for source creators flagged needs_enrichment.

    Commits per item so one failure cannot poison the batch; aborts early on
    DanbooruUnavailableError because every later item would fail the same way.
    """
    stmt = (
        select(SourceCreator)
        .where(SourceCreator.raw_metadata["_disk_import"]["needs_enrichment"].as_boolean().is_(True))
        .order_by(SourceCreator.created_at)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    items: list[dict[str, Any]] = []
    counts = {"found": 0, "not_found": 0, "errors": 0, "skipped": 0}
    aborted = False

    for idx, sc in enumerate(rows):
        creator = await db.get(Creator, sc.creator_id) if sc.creator_id else None
        if creator is None:
            counts["skipped"] += 1
            items.append({
                "source": sc.source,
                "source_creator_id": sc.source_creator_id,
                "status": "skipped_no_creator",
            })
            continue

        result = await enrich_creator_from_danbooru(db, creator, source_creator=sc)
        status = result["status"]
        if status == STATUS_FOUND:
            counts["found"] += 1
        elif status == STATUS_NOT_FOUND:
            counts["not_found"] += 1
        else:
            counts["errors"] += 1
        await db.commit()

        items.append({
            "source": sc.source,
            "source_creator_id": sc.source_creator_id,
            "creator_id": str(creator.id),
            "display_name": creator.display_name or creator.name,
            **result,
        })
        if progress_cb:
            progress_cb({"scanned": idx + 1, "total": len(rows), **counts})
        if status.startswith("danbooru_error:"):
            aborted = True
            break

    return {"scanned": len(items), "total": len(rows), **counts, "aborted": aborted, "items": items}
