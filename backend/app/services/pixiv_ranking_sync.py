"""Atomic persistence for one complete Pixiv daily ranking batch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from sqlalchemy import delete

from app.models.source_ranking_snapshot import SourceRankingSnapshot
from app.remote_discovery.pixiv import PIXIV_RANKING_MODES, PixivRankingResult
from app.services.work_heat import recompute_source_heat


class PixivRankingDateNotReady(RuntimeError):
    """Pixiv has not published a complete four-mode batch for the requested date."""


async def store_pixiv_ranking_results(
    db,
    results: Iterable[PixivRankingResult],
    *,
    recompute: Callable[..., Awaitable[set]] = recompute_source_heat,
) -> dict[str, int]:
    batch = tuple(results)
    modes = [result.mode for result in batch]
    ranking_dates = {result.ranking_date for result in batch}
    if (
        len(batch) != len(PIXIV_RANKING_MODES)
        or set(modes) != set(PIXIV_RANKING_MODES)
        or len(set(modes)) != len(modes)
        or len(ranking_dates) != 1
        or any(not result.items for result in batch)
    ):
        raise PixivRankingDateNotReady(
            "Pixiv ranking date has not advanced for every required mode"
        )

    for result in batch:
        expected_ranks = list(range(1, len(result.items) + 1))
        if [item.rank for item in result.items] != expected_ranks:
            raise ValueError("Pixiv ranking entries must have contiguous ranks")

    ranking_date = next(iter(ranking_dates))
    observed_at = max(result.fetched_at for result in batch)
    await db.execute(
        delete(SourceRankingSnapshot).where(
            SourceRankingSnapshot.source == "pixiv",
            SourceRankingSnapshot.ranking_date == ranking_date,
            SourceRankingSnapshot.mode.in_(PIXIV_RANKING_MODES),
        )
    )
    rows = [
        SourceRankingSnapshot(
            source="pixiv",
            mode=result.mode,
            ranking_date=result.ranking_date,
            source_work_id=item.source_work_id,
            rank=item.rank,
            rank_total=len(result.items),
            fetched_at=result.fetched_at,
        )
        for result in batch
        for item in result.items
    ]
    db.add_all(rows)
    await db.flush()
    changed_work_ids = await recompute(db, {"pixiv"}, now=observed_at)
    return {"snapshots": len(rows), "changed_works": len(changed_work_ids)}


__all__ = [
    "PixivRankingDateNotReady",
    "store_pixiv_ranking_results",
]
