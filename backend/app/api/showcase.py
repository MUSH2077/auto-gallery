"""Showcase sampling: a random window of visible works with signed preview URLs.

Deliberately avoids `ORDER BY random()` — that is a full scan of a ~36k-row
table. Instead: read the (cached) filtered total, pick a random offset, pull
one indexed window via the existing repository, then shuffle within the
window. Randomness comes from a fresh window per request plus the in-window
shuffle; cost stays an index scan.
"""
import random
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequirePermission
from app.database import get_db
from app.models.asset import Asset
from app.models.user import User
from app.repositories.work import WorkRepository
from app.schemas.showcase import ShowcaseItem, ShowcaseSampleResponse
from app.services.cache import cache_get, cache_key, cache_set
from app.services.media_signing import signed_media_url

_require_library = RequirePermission("library")

router = APIRouter()


@router.get("/sample", response_model=ShowcaseSampleResponse)
async def sample(
    count: int = Query(24, ge=1, le=60),
    scope: str = Query("all", pattern="^(all|favorites)$"),
    source: str | None = None,
    tag: str | None = None,
    include_nsfw: bool = False,
    user: User = _require_library,
    db: AsyncSession = Depends(get_db),
):
    # NSFW is double-gated: the account setting always wins over the request.
    force_sfw = (not user.nsfw_visible) or (not include_nsfw)
    is_favorite = True if scope == "favorites" else None

    count_ck = cache_key("showcase:count", source=source, tag=tag,
                         is_favorite=is_favorite, force_sfw=force_sfw)
    cached_total = cache_get(count_ck)

    repo = WorkRepository(db)
    # The first call establishes the total and doubles as the window when the
    # filtered library is smaller than `count`.
    works, total = await repo.list_all(
        offset=0, limit=count,
        source=source, tag=tag, is_favorite=is_favorite,
        curation_visibility="visible",
        precomputed_total=cached_total,
        force_sfw=force_sfw,
    )
    if cached_total is None:
        cache_set(count_ck, total, 300)

    if total > count:
        offset = random.randint(0, total - count)
        works, _ = await repo.list_all(
            offset=offset, limit=count,
            source=source, tag=tag, is_favorite=is_favorite,
            curation_visibility="visible",
            precomputed_total=total,
            force_sfw=force_sfw,
        )
        if not works:
            # Self-heal from a stale cached total: `total` above may have
            # come from the 300s count cache. If rows were deleted or hidden
            # since it was cached, `offset` can land past the real end of the
            # filtered set and this window comes back empty even though
            # matching works still exist. Retry once at offset 0 with
            # precomputed_total=None to force a fresh COUNT, and refresh the
            # cache with that fresh value so subsequent requests don't repeat
            # this. This extra query only runs on this rare stale-cache path
            # — the common path above is unaffected.
            works, total = await repo.list_all(
                offset=0, limit=count,
                source=source, tag=tag, is_favorite=is_favorite,
                curation_visibility="visible",
                precomputed_total=None,
                force_sfw=force_sfw,
            )
            cache_set(count_ck, total, 300)

    works = list(works)
    random.shuffle(works)

    asset_ids = [w.thumbnail_asset_id for w in works if w.thumbnail_asset_id]
    dims: dict[UUID, tuple[int | None, int | None]] = {}
    if asset_ids:
        rows = (await db.execute(
            select(Asset.id, Asset.width, Asset.height).where(Asset.id.in_(asset_ids))
        )).all()
        dims = {r.id: (r.width, r.height) for r in rows}

    items: list[ShowcaseItem] = []
    for w in works:
        if not w.thumbnail_asset_id:
            continue  # nothing to render — skip rather than emit a blank plane
        aid = str(w.thumbnail_asset_id)
        width, height = dims.get(w.thumbnail_asset_id, (None, None))
        items.append(ShowcaseItem(
            work_id=str(w.id),
            title=w.title,
            creator_name=getattr(w, "creator_name", None),
            source=getattr(w, "source", None),
            asset_id=aid,
            thumb_url=f"/media/thumb/{aid}",
            preview_url=signed_media_url(aid, "preview"),
            width=width,
            height=height,
        ))

    return ShowcaseSampleResponse(items=items)
