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


_CLAIM_PUBLISHER_FENCE_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 then
  return 0
end
local acquired = redis.call('set', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2])
if acquired then
  return 1
end
return 0
"""

_CLAIM_LEGACY_PUBLISHER_FENCE_SCRIPT = """
if redis.call('exists', KEYS[1]) == 1 or redis.call('exists', KEYS[3]) == 1 then
  return 0
end
local acquired = redis.call('set', KEYS[2], ARGV[1], 'NX', 'EX', ARGV[2])
if acquired then
  return 1
end
return 0
"""

_RELEASE_PUBLISHER_FENCE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_FENCED_HEARTBEAT_SCRIPT = """
if redis.call('exists', KEYS[2]) == 1 then
  return 0
end
redis.call('setex', KEYS[1], ARGV[1], ARGV[2])
redis.call('publish', KEYS[3], ARGV[3])
return 1
"""


class PublisherFenceError(RuntimeError):
    """A fenced publisher must stop before its next durable mutation."""

    def __init__(self, message: str, *, recovery_won: bool):
        super().__init__(message)
        self.recovery_won = recovery_won


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
    PUBLISHER_FENCE_TTL = 300

    @staticmethod
    def publisher_heartbeat_key(task_id: str, attempt_token: str) -> str:
        return f"task:{task_id}:publisher:{attempt_token}:heartbeat_ts"

    @staticmethod
    def publisher_fence_key(task_id: str, attempt_token: str) -> str:
        return f"task:{task_id}:publisher:{attempt_token}:fence"

    @staticmethod
    def try_claim_publisher_fence(
        redis_client,
        task_id: str,
        attempt_token: str,
        owner_token: str,
        *,
        check_legacy_heartbeat: bool = False,
    ) -> bool:
        """Atomically fence a publisher only while its heartbeat is absent."""

        if check_legacy_heartbeat:
            return bool(redis_client.eval(
                _CLAIM_LEGACY_PUBLISHER_FENCE_SCRIPT,
                3,
                TaskEventPublisher.publisher_heartbeat_key(
                    task_id,
                    attempt_token,
                ),
                TaskEventPublisher.publisher_fence_key(task_id, attempt_token),
                f"task:{task_id}:heartbeat_ts",
                owner_token,
                TaskEventPublisher.PUBLISHER_FENCE_TTL,
            ))
        return bool(redis_client.eval(
            _CLAIM_PUBLISHER_FENCE_SCRIPT,
            2,
            TaskEventPublisher.publisher_heartbeat_key(task_id, attempt_token),
            TaskEventPublisher.publisher_fence_key(task_id, attempt_token),
            owner_token,
            TaskEventPublisher.PUBLISHER_FENCE_TTL,
        ))

    @staticmethod
    def release_publisher_fence(
        redis_client,
        task_id: str,
        attempt_token: str,
        owner_token: str,
    ) -> bool:
        """Release only this scanner's fence; never delete a newer claim."""

        return bool(redis_client.eval(
            _RELEASE_PUBLISHER_FENCE_SCRIPT,
            1,
            TaskEventPublisher.publisher_fence_key(task_id, attempt_token),
            owner_token,
        ))

    @staticmethod
    def publish_heartbeat(
        task_id: str,
        task_type: str,
        *,
        pid: int | None = None,
        stage: str | None = None,
        fence_aware: bool = False,
        attempt_token: str | None = None,
        redis_client=None,
    ) -> bool:
        """Publish a worker heartbeat ping.

        Called every ~10s by the worker's heartbeat thread. The WebSocket
        manager and stale-detector use this to know the worker is alive.

        Also sets a TTL liveness key so the scheduler can check the worker
        without querying the database (the heartbeat thread cannot safely use
        async DB sessions). Fence-aware publishers use an attempt-scoped key;
        ordinary workers retain the legacy task-scoped key.
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
        r = redis_client if redis_client is not None else get_redis()
        if fence_aware:
            if not attempt_token:
                raise ValueError(
                    "fence-aware publisher heartbeat requires an attempt token"
                )
            return bool(r.eval(
                _FENCED_HEARTBEAT_SCRIPT,
                3,
                TaskEventPublisher.publisher_heartbeat_key(
                    task_id,
                    attempt_token,
                ),
                TaskEventPublisher.publisher_fence_key(task_id, attempt_token),
                TaskChannel.heartbeat(task_id),
                TaskEventPublisher.HEARTBEAT_TTL,
                event["timestamp"],
                payload,
            ))
        r.publish(TaskChannel.heartbeat(task_id), payload)
        # Set a TTL key so the stale detector can check liveness from Redis
        r.setex(
            f"task:{task_id}:heartbeat_ts",
            TaskEventPublisher.HEARTBEAT_TTL,
            event["timestamp"],
        )
        return True
