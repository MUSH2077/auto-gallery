"""RQ entry point for one bounded Meilisearch projection batch."""

from __future__ import annotations

import asyncio

from app.services.outbox_coordinator import clear_and_wake_outbox_successor
from app.services.stage_metrics import measure_stage


async def _run(limit: int, cooldown: dict[str, float]):
    from app.services.search_delivery import run_delivery_slice

    return await run_delivery_slice(limit=limit)


def run_search_projection_outbox(limit: int = 500):
    bounded = max(1, min(int(limit), 500))
    result = None
    cooldown: dict[str, float] = {}
    try:
        with measure_stage("search_projection_slice", limit=bounded):
            result = asyncio.run(_run(bounded, cooldown))
            if isinstance(result, dict):
                result["successor_delay_seconds"] = max(
                    float(result.get("successor_delay_seconds") or 0),
                    cooldown.get("seconds", 0.0),
                )
            return result
    finally:
        clear_and_wake_outbox_successor("search", result)
