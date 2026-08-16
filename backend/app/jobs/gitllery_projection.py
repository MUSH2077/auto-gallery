"""RQ entry point for bounded Gitllery projection slices."""

from __future__ import annotations

import asyncio

from app.config import settings
from app.services.gitllery_outbox import process_gitllery_projection_outbox
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
        owner = str(current.id) if current is not None else "gitllery-projection"
    except Exception:
        owner = "gitllery-projection"
    async with adaptive_resource_slice(
        "git_projection",
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
        return await process_gitllery_projection_outbox(
            limit=min(limit, limits.work_units),
            max_seconds=min(max_seconds, limits.slice_seconds or max_seconds),
        )


def run_gitllery_projection_outbox(limit: int = 25, max_seconds: float = 20.0):
    if settings.gitllery_projection_mode.strip().lower() != "active":
        result = {
            "claimed": 0,
            "processed": 0,
            "failed": 0,
            "shadow_only": True,
            "more_likely": False,
        }
        clear_and_wake_outbox_successor("gitllery", result)
        return result
    result = None
    cooldown: dict[str, float] = {}
    try:
        with measure_stage("gitllery_projection_slice", limit=limit):
            result = asyncio.run(_run_projection(limit, max_seconds, cooldown))
            if isinstance(result, dict):
                result["successor_delay_seconds"] = cooldown.get("seconds", 0.0)
            return result
    finally:
        clear_and_wake_outbox_successor("gitllery", result)
