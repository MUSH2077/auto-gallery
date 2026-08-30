#!/usr/bin/env python3
"""Verify fresh RQ registration coverage for the production worker topology."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import sys
from typing import Any


ROLE_QUEUES = {
    "download": {
        "downloads",
        "downloads:pixiv",
        "downloads:danbooru",
        "downloads:iwara",
        "downloads:weibo",
        "downloads:bilibili",
        "downloads:pinterest",
        "downloads:lofter",
        "downloads:x",
    },
    # Extra queues are separate RQ child registrations in the same service.
    "import": {"imports", "maintenance"},
    "operations": {"operations", "discovery"},
    "scheduler": {"scheduled"},
}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_registrations(
    payload: Any,
    *,
    max_age_seconds: float = 480.0,
) -> tuple[bool, str]:
    """Return whether fresh registrations cover every service-owned queue.

    RQ can block in dequeue for roughly seven minutes, so the default accepts
    the normal heartbeat cadence while still rejecting expired registrations.
    """

    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(float(max_age_seconds))
        or float(max_age_seconds) <= 0
    ):
        return False, "invalid heartbeat maximum age"
    if not isinstance(payload, dict):
        return False, "registration payload is not an object"
    observed_at = _parse_timestamp(payload.get("observed_at"))
    workers = payload.get("workers")
    if observed_at is None or not isinstance(workers, list):
        return False, "registration payload is incomplete"

    fresh_queues: set[str] = set()
    stale_queues: set[str] = set()
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        queues = worker.get("queues")
        heartbeat = _parse_timestamp(worker.get("last_heartbeat"))
        if not isinstance(queues, list) or heartbeat is None:
            continue
        normalized = {name for name in queues if isinstance(name, str) and name}
        age = (observed_at - heartbeat).total_seconds()
        if -30.0 <= age <= float(max_age_seconds):
            fresh_queues.update(normalized)
        else:
            stale_queues.update(normalized)

    failures: list[str] = []
    for role, required in ROLE_QUEUES.items():
        missing = sorted(required - fresh_queues)
        if missing:
            stale = sorted(set(missing) & stale_queues)
            detail = f"{role} missing fresh queues: {','.join(missing)}"
            if stale:
                detail += f" (stale: {','.join(stale)})"
            failures.append(detail)
    if failures:
        return False, "; ".join(failures)
    return True, f"fresh RQ queue coverage complete ({len(fresh_queues)} queues)"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        max_age = float(os.environ.get("VERIFY_RQ_HEARTBEAT_MAX_AGE_SECONDS", "480"))
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError):
        print("RQ registration payload unavailable or invalid", file=sys.stderr)
        return 1
    ready, summary = evaluate_registrations(payload, max_age_seconds=max_age)
    print(summary, file=sys.stderr)
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
