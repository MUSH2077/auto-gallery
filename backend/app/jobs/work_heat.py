"""RQ boundary for materialized source/work heat recomputation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.database import async_session
from app.models.source_ranking_snapshot import SourceRankingSnapshot
from app.services.work_heat import official_rank_expired, recompute_source_heat
from app.services.work_heat_queue import request_work_heat_recompute


async def _run_work_heat_recompute(source: str) -> dict[str, int | str]:
    async with async_session() as db:
        changed = await recompute_source_heat(db, {source})
        await db.commit()
        return {
            "status": "completed",
            "source": source,
            "changed_works": len(changed),
        }


def run_work_heat_recompute(source: str):
    return asyncio.run(_run_work_heat_recompute(source.strip().lower()))


async def _latest_pixiv_ranking_fetched_at() -> datetime | None:
    async with async_session() as db:
        return (
            await db.execute(
                select(func.max(SourceRankingSnapshot.fetched_at)).where(
                    SourceRankingSnapshot.source == "pixiv"
                )
            )
        ).scalar_one_or_none()


def expire_pixiv_heat():
    """Queue recomputation only when the newest persisted Pixiv rank is stale."""

    latest_fetched_at = asyncio.run(_latest_pixiv_ranking_fetched_at())
    now = datetime.now(UTC)
    if latest_fetched_at is None:
        return {"status": "skipped", "reason": "no_ranking_snapshot"}
    if not official_rank_expired(latest_fetched_at, now):
        return {
            "status": "skipped",
            "reason": "newer_ranking_is_fresh",
            "latest_fetched_at": latest_fetched_at.isoformat(),
        }
    queued = request_work_heat_recompute({"pixiv"})
    if queued["errors"]:
        # This job carries an RQ Retry policy.  Raising keeps a transient Redis
        # failure from turning the only exact 48-hour expiry check into success.
        raise RuntimeError("Pixiv heat expiry recomputation could not be queued")
    return {
        "status": "queued" if queued["created"] else "coalesced",
        "latest_fetched_at": latest_fetched_at.isoformat(),
        **queued,
    }


__all__ = ["expire_pixiv_heat", "run_work_heat_recompute"]
