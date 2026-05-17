"""Creator deduplication helpers — find existing creators by identity markers."""

import logging
from uuid import UUID

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Creator, CreatorLink, SourceCreator

logger = logging.getLogger(__name__)


async def find_existing_creator(
    db: AsyncSession,
    *,
    danbooru_artist_id: int | None = None,
    source: str | None = None,
    source_creator_id: str | None = None,
    source_url: str | None = None,
) -> Creator | None:
    """Find an existing creator by identity markers.

    Checks in priority order:
    1. danbooru_artist_id match (most reliable)
    2. SourceCreator source+source_creator_id match
    3. CreatorLink URL match (same URL pointing to another creator)
    """
    # 1. Danbooru artist ID — gold standard
    if danbooru_artist_id is not None:
        result = await db.execute(
            select(Creator).where(Creator.danbooru_artist_id == danbooru_artist_id)
        )
        creator = result.scalar_one_or_none()
        if creator:
            logger.info("Found existing creator %s by danbooru_artist_id=%d",
                        creator.id, danbooru_artist_id)
            return creator

    # 2. SourceCreator match — same platform + platform ID
    if source and source_creator_id:
        result = await db.execute(
            select(Creator).join(SourceCreator).where(
                SourceCreator.source == source,
                SourceCreator.source_creator_id == source_creator_id,
            )
        )
        creator = result.scalar_one_or_none()
        if creator:
            logger.info("Found existing creator %s by source_creator %s:%s",
                        creator.id, source, source_creator_id)
            return creator

    # 3. CreatorLink URL match — same URL claimed by a different creator
    if source_url:
        result = await db.execute(
            select(CreatorLink).where(CreatorLink.url == source_url)
        )
        link = result.scalar_one_or_none()
        if link and link.creator_id:
            result2 = await db.execute(
                select(Creator).where(Creator.id == link.creator_id)
            )
            creator = result2.scalar_one_or_none()
            if creator:
                logger.info("Found existing creator %s by link URL %s",
                            creator.id, source_url)
                return creator

    return None


async def find_merge_candidates(db: AsyncSession, limit: int = 50) -> list[dict]:
    """Find potential duplicate creators (same name, same source creator, etc.)."""
    candidates = []

    # Strategy A: Same danbooru_artist_id
    from sqlalchemy import func
    result = await db.execute(
        select(
            Creator.danbooru_artist_id,
            func.count(Creator.id).label("cnt"),
            func.array_agg(Creator.id).label("ids"),
            func.array_agg(Creator.name).label("names"),
        )
        .where(Creator.danbooru_artist_id.isnot(None))
        .group_by(Creator.danbooru_artist_id)
        .having(func.count(Creator.id) > 1)
        .limit(limit)
    )
    for row in result:
        candidates.append({
            "reason": "same_danbooru_artist_id",
            "description": f"Danbooru artist #{row.danbooru_artist_id}",
            "creator_ids": [str(uid) for uid in row.ids],
            "creator_names": list(row.names),
        })

    # Strategy B: Same source_creator_id
    if len(candidates) < limit:
        result = await db.execute(
            select(
                SourceCreator.source,
                SourceCreator.source_creator_id,
                func.count(func.distinct(SourceCreator.creator_id)).label("cnt"),
                func.array_agg(func.distinct(SourceCreator.creator_id)).label("creator_ids"),
            )
            .where(SourceCreator.creator_id.isnot(None))
            .group_by(SourceCreator.source, SourceCreator.source_creator_id)
            .having(func.count(func.distinct(SourceCreator.creator_id)) > 1)
            .limit(limit - len(candidates))
        )
        for row in result:
            # Get names
            creator_uuid = UUID(row.creator_ids[0]) if row.creator_ids else None
            names = []
            for cid in row.creator_ids:
                c = await db.get(Creator, UUID(cid))
                if c:
                    names.append(c.name)
            candidates.append({
                "reason": "same_source_creator",
                "description": f"{row.source}:{row.source_creator_id} mapped to multiple creators",
                "creator_ids": [str(uid) for uid in row.creator_ids],
                "creator_names": names,
            })

    return candidates


async def merge_creators(db: AsyncSession, target_id: UUID, source_id: UUID) -> dict:
    """Merge source creator into target creator.

    Moves all links, source_creators, and subscriptions to target.
    Deletes source creator after merge.
    """
    if target_id == source_id:
        raise ValueError("Cannot merge a creator into itself")

    target = await db.get(Creator, target_id)
    source = await db.get(Creator, source_id)
    if not target or not source:
        raise ValueError("One or both creators not found")

    stats = {"links_moved": 0, "source_creators_moved": 0, "subscriptions_moved": 0}

    # Move creator_links
    result = await db.execute(
        select(CreatorLink).where(CreatorLink.creator_id == source_id)
    )
    for link in result.scalars().all():
        # Check if target already has this URL
        existing = await db.execute(
            select(CreatorLink).where(
                CreatorLink.creator_id == target_id,
                CreatorLink.url == link.url,
            )
        )
        if existing.scalar_one_or_none():
            await db.delete(link)
            continue
        link.creator_id = target_id
        stats["links_moved"] += 1

    # Move source_creators
    result = await db.execute(
        select(SourceCreator).where(SourceCreator.creator_id == source_id)
    )
    for sc in result.scalars().all():
        existing = await db.execute(
            select(SourceCreator).where(
                SourceCreator.source == sc.source,
                SourceCreator.source_creator_id == sc.source_creator_id,
                SourceCreator.creator_id == target_id,
            )
        )
        if existing.scalar_one_or_none():
            await db.delete(sc)
            continue
        sc.creator_id = target_id
        stats["source_creators_moved"] += 1

    # Transfer danbooru_artist_id if target doesn't have one
    if not target.danbooru_artist_id and source.danbooru_artist_id:
        target.danbooru_artist_id = source.danbooru_artist_id

    # Merge description
    if source.description and source.description not in (target.description or ""):
        target.description = (target.description or "") + "\n" + source.description

    # Delete source creator
    await db.delete(source)
    await db.commit()

    logger.info("Merged creator %s → %s: %s", source_id, target_id, stats)
    return stats
