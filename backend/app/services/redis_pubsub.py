"""
Redis Pub/Sub infrastructure for the Task Engine.

Provides:
  - TaskChannel: canonical channel name helpers
  - TaskEventPublisher: publish status changes, progress updates, and
    worker control signals to Redis channels

All publishers use the shared Redis connection pool from
``app.services.redis_client.get_redis()``.

Channel naming convention: ``task:{scope}:{detail}``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.services.redis_client import get_redis


# ──────────────────────────────────────────────
# Channel definitions
# ──────────────────────────────────────────────

class TaskChannel:
    """Canonical Redis pub/sub channel names for the Task Engine.

    Usage::

        channel = TaskChannel.control(job_id)
        get_redis().publish(channel, json.dumps({"command": "pause"}))
    """

    @staticmethod
    def control(task_id: str) -> str:
        """Worker listens on this channel for pause/cancel/resume commands."""
        return f"task:{task_id}:control"

    @staticmethod
    def heartbeat(task_id: str) -> str:
        """Worker publishes heartbeat pings on this channel."""
        return f"task:{task_id}:heartbeat"

    @staticmethod
    def progress(task_id: str) -> str:
        """Worker publishes progress snapshots on this channel."""
        return f"task:{task_id}:progress"

    @staticmethod
    def task_events(task_type: str) -> str:
        """Per-type event stream. ``task_type`` is ``"download"`` or ``"import"``."""
        return f"task:{task_type}:events"

    @staticmethod
    def all_events() -> str:
        """Global event stream — WebSocket server subscribes here."""
        return "task:all:events"


# ──────────────────────────────────────────────
# Event publisher
# ──────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskEventPublisher:
    """Publishes structured task lifecycle events to Redis pub/sub.

    Every event is a JSON object with at minimum::

        {"type": "<event_type>", "task_id": "...", "task_type": "download|import",
         "timestamp": "<iso8601>"}
    """

    # ── status change ──────────────────────────────

    @staticmethod
    def publish_status_change(
        task_id: str,
        task_type: str,
        old_status: str,
        new_status: str,
        *,
        operator: str | None = None,
        note: str | None = None,
        progress: dict[str, Any] | None = None,
    ) -> None:
        """Publish a status transition event to per-type and global channels."""
        event: dict[str, Any] = {
            "type": "status_change",
            "task_id": task_id,
            "task_type": task_type,
            "old_status": old_status,
            "new_status": new_status,
            "timestamp": _now_iso(),
        }
        if operator:
            event["operator"] = operator
        if note:
            event["note"] = note
        if progress:
            event["progress"] = progress

        payload = json.dumps(event, ensure_ascii=False)
        r = get_redis()
        r.publish(TaskChannel.task_events(task_type), payload)
        r.publish(TaskChannel.all_events(), payload)

    # ── progress ───────────────────────────────────

    @staticmethod
    def publish_progress(
        task_id: str,
        task_type: str,
        progress: dict[str, Any],
    ) -> None:
        """Publish a progress update. ``progress`` should contain at minimum
        ``{"stage": str, "current": int, "total": int, "percent": float}``.
        """
        event = {
            "type": "progress",
            "task_id": task_id,
            "task_type": task_type,
            "progress": progress,
            "timestamp": _now_iso(),
        }
        payload = json.dumps(event, ensure_ascii=False)
        r = get_redis()
        # Per-task progress channel (narrow)
        r.publish(TaskChannel.progress(task_id), payload)
        # Also to per-type and global for dashboard updates
        r.publish(TaskChannel.task_events(task_type), payload)
        r.publish(TaskChannel.all_events(), payload)

    # ── control signals ────────────────────────────

    @staticmethod
    def send_control(
        task_id: str,
        command: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Send a control command to a running worker.

        ``command`` must be one of: ``"pause"``, ``"cancel"``, ``"resume"``.

        The worker's control-listener thread receives this and acts on it
        (e.g. SIGTERM the gallery-dl process group).
        """
        msg: dict[str, Any] = {
            "command": command,
            "timestamp": _now_iso(),
        }
        if reason:
            msg["reason"] = reason

        payload = json.dumps(msg, ensure_ascii=False)
        r = get_redis()
        r.publish(TaskChannel.control(task_id), payload)
        # Also set a Redis key for poll-based consumers (import workers)
        r.setex(f"task:{task_id}:signal", 120, payload)

    # ── heartbeat ──────────────────────────────────

    HEARTBEAT_TTL = 90  # Redis key TTL — must exceed stale detection window

    @staticmethod
    def publish_heartbeat(
        task_id: str,
        task_type: str,
        *,
        pid: int | None = None,
        stage: str | None = None,
    ) -> None:
        """Publish a worker heartbeat ping.

        Called every ~10s by the worker's heartbeat thread. The WebSocket
        manager and stale-detector use this to know the worker is alive.

        Also sets a Redis key ``task:{task_id}:heartbeat_ts`` with a TTL so
        that the scheduler's stale-detection can check liveness without
        querying the database (the heartbeat thread cannot safely use
        async DB sessions).
        """
        event: dict[str, Any] = {
            "type": "heartbeat",
            "task_id": task_id,
            "task_type": task_type,
            "timestamp": _now_iso(),
        }
        if pid is not None:
            event["pid"] = pid
        if stage is not None:
            event["stage"] = stage

        payload = json.dumps(event, ensure_ascii=False)
        r = get_redis()
        r.publish(TaskChannel.heartbeat(task_id), payload)
        # Set a TTL key so the stale detector can check liveness from Redis
        r.setex(
            f"task:{task_id}:heartbeat_ts",
            TaskEventPublisher.HEARTBEAT_TTL,
            _now_iso(),
        )
