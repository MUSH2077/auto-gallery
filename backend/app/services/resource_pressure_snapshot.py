"""Redis publication and reading of the short-lived resource pressure snapshot."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

PRESSURE_SNAPSHOT_KEY = "resource:pressure:snapshot"
RESOURCE_CONTROL_CHANNEL = "resource:control"

_publish_lock = threading.Lock()
_last_published_signature: tuple[Any, ...] | None = None
_last_published_at = 0.0
_last_publish_client_id: int | None = None


def _redis_client(redis_client=None):
    if redis_client is not None:
        return redis_client
    from app.services.redis_client import get_redis

    return get_redis()


def publish_resource_pressure_snapshot(snapshot: dict[str, Any], redis_client=None) -> bool:
    global _last_published_signature, _last_published_at, _last_publish_client_id
    try:
        client = _redis_client(redis_client)
        budget = snapshot.get("budget") or {}
        profiles = budget.get("profiles") or {}
        reservation = budget.get("reservation") or {}
        signature = (
            snapshot.get("status"),
            snapshot.get("controller_mode"),
            tuple(snapshot.get("reasons") or []),
            tuple((snapshot.get("local_cgroup_warnings") or {}).get("reasons") or []),
            (snapshot.get("local_cgroup_warnings") or {}).get("max_delta"),
            (snapshot.get("local_cgroup_warnings") or {}).get("oom_delta"),
            budget.get("generation"),
            budget.get("throughput_scale"),
            budget.get("governance_mode"),
            reservation.get("active_count"),
            reservation.get("reserved_bytes"),
            tuple(
                sorted(
                    str(value.get("token"))
                    for value in (reservation.get("active_leases") or [])
                    if isinstance(value, dict)
                )
            ),
            tuple(
                (name, value.get("allowed"), value.get("reason"))
                for name, value in sorted(profiles.items())
                if isinstance(value, dict)
            ),
        )
        now = time.monotonic()
        ttl = max(10, settings.resource_pressure_snapshot_ttl_seconds)
        client_id = id(client)
        with _publish_lock:
            changed = (
                signature != _last_published_signature
                or client_id != _last_publish_client_id
            )
            # Local sampling remains responsive at 5s, while stable Redis/AOF
            # traffic is capped at two snapshot writes per minute.
            heartbeat_due = now - _last_published_at >= min(30.0, ttl * 0.75)
            if not changed and not heartbeat_due:
                return True
        client.set(
            PRESSURE_SNAPSHOT_KEY,
            json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True),
            ex=ttl,
        )
        with _publish_lock:
            _last_published_signature = signature
            _last_published_at = now
            _last_publish_client_id = client_id
        if changed:
            try:
                client.publish(
                    RESOURCE_CONTROL_CHANNEL,
                    json.dumps(
                        {
                            "type": "resource_budget_changed",
                            "status": snapshot.get("status"),
                            "controller_mode": snapshot.get("controller_mode"),
                            "generation": budget.get("generation"),
                        },
                        separators=(",", ":"),
                    ),
                )
            except Exception:
                logger.debug("Unable to publish resource control event", exc_info=True)
        return True
    except Exception:
        logger.debug("Unable to publish resource pressure snapshot", exc_info=True)
        return False


def read_shared_resource_pressure_snapshot(redis_client=None) -> dict[str, Any] | None:
    try:
        raw = _redis_client(redis_client).get(PRESSURE_SNAPSHOT_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("status") not in {"normal", "warning", "paused"}:
            return None
        value.setdefault("reasons", [])
        return value
    except Exception:
        return None


