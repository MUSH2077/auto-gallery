"""RQ boundary for materialized source/work heat recomputation."""

from __future__ import annotations

import asyncio

from app.database import async_session
from app.services.work_heat import recompute_source_heat


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


__all__ = ["run_work_heat_recompute"]
