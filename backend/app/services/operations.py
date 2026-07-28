"""Shared status storage for background admin operations."""

from __future__ import annotations

import json
import time
from typing import Any

from app.services.redis_client import get_redis

OPERATION_TTL_SECONDS = 7200


def operation_key(job_id: str) -> str:
    return f"admin_operation:{job_id}"


def set_operation_status(
    job_id: str,
    status: str,
    operation_type: str,
    *,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "operation_type": operation_type,
        "updated_at": time.time(),
    }
    if progress is not None:
        payload["progress"] = progress
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    if meta is not None:
        payload["meta"] = meta

    get_redis().setex(
        operation_key(job_id),
        OPERATION_TTL_SECONDS,
        json.dumps(payload, default=str),
    )
    return payload


def get_operation_status(job_id: str) -> dict[str, Any] | None:
    raw = get_redis().get(operation_key(job_id))
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)
