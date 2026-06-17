"""
Progress tracking — Redis-backed progress snapshots for download/import jobs.

Progress is written by workers via ``TaskEventPublisher.publish_progress()``
and can be read by the API or WebSocket subscribers.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.redis_client import get_redis


class ProgressTracker:
    """Reads and writes progress snapshots in Redis.

    Progress keys have a short TTL (30s) — they are ephemeral snapshots,
    not permanent state. For historical data, use the job manifest in PostgreSQL.
    """

    PROGRESS_KEY_PREFIX = "task"
    PROGRESS_TTL = 30  # seconds

    @staticmethod
    def get(task_id: str) -> dict[str, Any] | None:
        """Get the latest progress snapshot for a task."""
        try:
            raw = get_redis().get(f"{ProgressTracker.PROGRESS_KEY_PREFIX}:{task_id}:progress")
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    @staticmethod
    def set(task_id: str, progress: dict[str, Any]) -> None:
        """Store a progress snapshot with a short TTL."""
        try:
            get_redis().setex(
                f"{ProgressTracker.PROGRESS_KEY_PREFIX}:{task_id}:progress",
                ProgressTracker.PROGRESS_TTL,
                json.dumps(progress, ensure_ascii=False),
            )
        except Exception:
            pass
