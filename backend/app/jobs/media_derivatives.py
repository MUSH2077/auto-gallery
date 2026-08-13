"""RQ entry point for bounded media derivative slices."""

from __future__ import annotations

import asyncio

from app.services.media_derivatives import process_media_derivative_outbox
from app.services.outbox_coordinator import clear_and_wake_outbox_successor
from app.services.stage_metrics import measure_stage


def run_media_derivative_outbox(limit: int = 25, max_seconds: float = 20.0):
    result = None
    cooldown: dict[str, float] = {}
    try:
        with measure_stage("media_derivative_slice", limit=limit):
            result = asyncio.run(
                process_media_derivative_outbox(
                    limit=limit,
                    max_seconds=max_seconds,
                    cooldown_result=cooldown,
                )
            )
            if isinstance(result, dict):
                result["successor_delay_seconds"] = cooldown.get("seconds", 0.0)
            return result
    finally:
        clear_and_wake_outbox_successor("media", result)
