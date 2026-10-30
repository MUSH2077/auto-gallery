"""RQ entry point for coalesced import curation projection."""

from __future__ import annotations

from typing import Any

import asyncio

from app.services.import_projection import process_import_projection_outbox
from app.services.outbox_coordinator import clear_and_wake_outbox_successor
from app.services.stage_metrics import measure_stage


async def _run_projection(
    limit: int,
    max_seconds: float,
    cooldown: dict[str, float],
):
    return await _run_split_projection(limit, max_seconds, cooldown)


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


async def _run_split_projection(limit: int, max_seconds: float, cooldown: dict[str, float]) -> dict[str, Any]:
    from time import monotonic
    from rq import get_current_job
    from app.services.heavy_io import adaptive_resource_slice, background_turn_available
    from app.services.redis_client import get_redis
    from app.services.import_projection import process_import_projection_outbox

    current = get_current_job()
    owner = str(current.id) if current is not None else "import-projection"
    deadline = monotonic() + max(0.001, float(max_seconds))
    bounded_limit = min(25, max(1, int(limit)))
    curation_cooldown, metadata_cooldown = {}, {}
    curation = None
    async with adaptive_resource_slice(
        "import_db", owner+":curation", max_work_units=bounded_limit,
        max_slice_seconds=max(0.001, deadline-monotonic()),
        cooldown_result=curation_cooldown, wait_for_capacity=False, lane="completion",
    ) as limits:
        if limits is None:
            return {"claimed":0,"processed":0,"failed":0,"resource_deferred":True,"more_likely":False}
        curation = await process_import_projection_outbox(
            limit=min(bounded_limit, limits.work_units),
            max_seconds=min(max(0.001, deadline-monotonic()), limits.slice_seconds or max_seconds),
            projection_part="curation",
        )
    result = {k:v for k,v in curation.items() if k != "claimed_ids"}
    cooldown["seconds"] = curation_cooldown.get("seconds", 0.0)
    if monotonic() >= deadline:
        return {**result,"more_likely":True}
    # Curation is a short completion step and may run while ingestion is busy.
    # Keep the existing background-turn gate for metadata and other maintenance.
    if not background_turn_available(get_redis()):
        return {**result,"resource_deferred":True,"deferred_projection_part":"metadata","more_likely":False}
    preferred = curation.get("claimed_ids") or None
    # Keep room for the normal empty confirmation after the preferred batch.
    # preferred IDs bound distinct work, rather than an exactly-full limit.
    metadata_limit = bounded_limit
    async with adaptive_resource_slice(
        "import_db", owner+":metadata", max_work_units=metadata_limit,
        max_slice_seconds=max(0.001, deadline-monotonic()),
        cooldown_result=metadata_cooldown, wait_for_capacity=False, lane="background",
    ) as limits:
        if limits is None:
            return {**result,"resource_deferred":True,"deferred_projection_part":"metadata","more_likely":False}
        metadata = await process_import_projection_outbox(
            limit=min(metadata_limit, limits.work_units),
            max_seconds=min(max(0.001, deadline-monotonic()), limits.slice_seconds or max_seconds),
            projection_part="metadata", preferred_metadata_ids=preferred,
        )
    if not curation["claimed"]:
        result["claimed"] = metadata["claimed"]
    for key in ("metadata_batches","metadata_processed","metadata_failed"):
        result[key] = metadata[key]
    result["more_likely"] = bool(curation["more_likely"] or metadata["more_likely"])
    cooldown["seconds"] = max(
        curation_cooldown.get("seconds",0.0), metadata_cooldown.get("seconds",0.0),
    )
    return result
