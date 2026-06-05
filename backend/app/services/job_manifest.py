from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "cookie", "cookies", "refresh_token"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            lower = str(key).lower()
            if any(s in lower for s in SENSITIVE_KEYS):
                clean[key] = "***REDACTED***"
            else:
                clean[key] = _redact(item)
        return clean
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def redacted_manifest_config(config: dict | None) -> dict:
    return _redact(config or {})


def get_manifest(job) -> dict:
    manifest = dict(job.manifest or {})
    manifest.setdefault("version", 1)
    manifest.setdefault("events", [])
    return manifest


def update_manifest(job, **fields) -> dict:
    manifest = get_manifest(job)
    for key, value in fields.items():
        manifest[key] = _redact(value)
    job.manifest = manifest
    return manifest


def append_manifest_event(job, event: str, **payload) -> dict:
    manifest = get_manifest(job)
    events = list(manifest.get("events") or [])
    events.append({"at": _now(), "event": event, **_redact(payload)})
    manifest["events"] = events[-200:]
    job.manifest = manifest
    return manifest
