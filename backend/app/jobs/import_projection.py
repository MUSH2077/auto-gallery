"""RQ entry point for coalesced import curation projection."""

from __future__ import annotations

import asyncio

from app.services.import_projection import process_import_projection_outbox
from app.services.outbox_coordinator import clear_and_wake_outbox_successor
from app.services.stage_metrics import measure_stage


async def _run_projection(
    limit: int,
    max_seconds: float,
    cooldown: dict[str, float],
):
    from app.services.heavy_io import adaptive_resource_slice

    try:
        from rq import get_current_job

        current = get_current_job()
        owner = str(current.id) if current is not None else "import-projection"
    except Exception:
        owner = "import-projection"
    async with adaptive_resource_slice(
        "import_db",
        owner,
        max_work_units=limit,
        max_slice_seconds=max_seconds,
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
        return await process_import_projection_outbox(
            limit=min(limit, limits.work_units),
            max_seconds=min(max_seconds, limits.slice_seconds or max_seconds),
        )


def run_import_projection_outbox(limit: int = 25, max_seconds: float = 20.0):
    result = None
    cooldown: dict[str, float] = {}
    try:
        with measure_stage("import_projection_slice", limit=limit):
            result = asyncio.run(_run_projection(limit, max_seconds, cooldown))
            if isinstance(result, dict):
                result["successor_delay_seconds"] = cooldown.get("seconds", 0.0)
            return result
    finally:
        clear_and_wake_outbox_successor("import_projection", result)
