#!/usr/bin/env python3
"""Container health probe for the supervised RQ worker processes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
from datetime import datetime, timezone

import redis


WORKER_SUPERVISOR_HASH_KEY = "worker:supervisors:v1"


def _decode_payload(raw) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _payload_health(payload: dict, hostname: str) -> tuple[bool, str]:
    if payload.get("hostname") != hostname:
        return False, "different host"
    valid_until = payload.get("valid_until")
    if valid_until:
        try:
            expires = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
            if expires <= datetime.now(timezone.utc):
                return False, "supervisor heartbeat expired"
        except (TypeError, ValueError):
            return False, "invalid supervisor expiry"
    state = payload.get("state")
    worker_count = int(payload.get("worker_count") or 0)
    expected = max(1, int(payload.get("expected_worker_count") or 1))
    if state == "running" and worker_count >= expected:
        return True, "ok"
    return False, f"state={state} workers={worker_count}/{expected}"


def evaluate_worker_health(
    client,
    *,
    hostname: str | None = None,
    require_gallery_dl: bool = False,
) -> tuple[bool, str]:
    """Evaluate the supervisor heartbeat without mutating Redis."""

    if require_gallery_dl and not shutil.which("gallery-dl"):
        return False, "gallery-dl is unavailable"

    hostname = hostname or socket.gethostname()
    hgetall = getattr(client, "hgetall", None)
    try:
        current = hgetall(WORKER_SUPERVISOR_HASH_KEY) if callable(hgetall) else {}
    except Exception as exc:  # noqa: BLE001 - health probe must return a clean failure
        return False, f"Redis heartbeat read failed: {type(exc).__name__}"

    errors: list[str] = []
    for raw in (current or {}).values():
        payload = _decode_payload(raw)
        if payload is None:
            errors.append("invalid supervisor heartbeat")
            continue
        healthy, message = _payload_health(payload, hostname)
        if healthy:
            return True, message
        if message == "different host":
            continue
        errors.append(message)

    # Rolling-upgrade fallback for the former per-host string keys.  New
    # supervisors only publish the fixed-size hash above (no Redis SCAN in the
    # steady state), but accepting the old key prevents a false unhealthy
    # transition while a container is being replaced.
    try:
        keys = list(
            client.scan_iter(
                match=f"worker:supervisor:{hostname}:*",
                count=10,
            )
        )
    except Exception:
        keys = []
    for key in keys:
        payload = _decode_payload(client.get(key))
        if payload is None:
            errors.append("invalid legacy supervisor heartbeat")
            continue
        state = payload.get("state")
        worker_count = int(payload.get("worker_count") or 0)
        if state == "running" and worker_count >= 1:
            return True, "ok"
        errors.append(f"state={state} workers={worker_count}")

    return False, "; ".join(errors) or "worker supervisor heartbeat is missing"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-gallery-dl", action="store_true")
    args = parser.parse_args()

    try:
        client = redis.from_url(
            os.environ["REDIS_URL"],
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        healthy, message = evaluate_worker_health(
            client,
            require_gallery_dl=args.require_gallery_dl,
        )
    except Exception as exc:  # noqa: BLE001 - Docker needs a non-zero result, not a traceback
        healthy, message = False, f"worker health probe failed: {type(exc).__name__}"
    if not healthy:
        print(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
