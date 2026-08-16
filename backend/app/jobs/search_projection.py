"""RQ entry point for one bounded Meilisearch projection batch."""

from __future__ import annotations

import asyncio

from app.database import async_session
from app.services.outbox_coordinator import clear_and_wake_outbox_successor
from app.services.stage_metrics import measure_stage


async def _run(limit: int, cooldown: dict[str, float]):
    from app.services.search import SearchService
    from app.services.heavy_io import adaptive_resource_slice

    try:
        from rq import get_current_job

        current = get_current_job()
        owner = str(current.id) if current is not None else "search-projection"
    except Exception:
        owner = "search-projection"

    async with adaptive_resource_slice(
        "search_index",
        owner,
        max_work_units=limit,
        max_slice_seconds=20.0,
        cooldown_result=cooldown,
        wait_for_capacity=False,
    ) as limits:
        if limits is None:
            return {
                "claimed": 0,
                "processed": 0,
                "failed": 0,
                "resource_deferred": True,
                "more_likely": False,
            }
        async with async_session() as db:
            return await SearchService(db).drain_search_projection_outbox(
                limit=min(limit, limits.work_units)
            )


def run_search_projection_outbox(limit: int = 500):
    bounded = max(1, min(int(limit), 500))
    result = None
    cooldown: dict[str, float] = {}
    try:
        with measure_stage("search_projection_slice", limit=bounded):
            result = asyncio.run(_run(bounded, cooldown))
            if isinstance(result, dict):
                result["successor_delay_seconds"] = cooldown.get("seconds", 0.0)
            return result
    finally:
        clear_and_wake_outbox_successor("search", result)
