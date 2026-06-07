"""Redis-based API response cache with TTL and pattern-based invalidation."""

import json
import logging
from typing import Any

import redis as _redis

from app.config import settings

logger = logging.getLogger(__name__)

CACHE_PREFIX = "cache:api:"
DEFAULT_TTL = 300  # 5 minutes


def _client() -> _redis.Redis:
    return _redis.from_url(settings.redis_url)


def _to_json(value: Any) -> Any:
    """Recursively convert Pydantic models to JSON-serializable dicts."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    return value


def cache_key(prefix: str, **params) -> str:
    """Build a deterministic cache key from a prefix and query parameters."""
    raw = json.dumps(dict(sorted(params.items())), sort_keys=True, default=str)
    return f"{CACHE_PREFIX}{prefix}:{raw}"


def cache_get(key: str) -> Any | None:
    """Get a cached value, or None if missing/expired."""
    try:
        r = _client()
        raw = r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        logger.debug("Cache read failed for key %s", key, exc_info=True)
        return None


def cache_set(key: str, value: Any, ttl_seconds: int = DEFAULT_TTL) -> None:
    """Set a cache value with TTL. Best-effort.

    Pydantic models are recursively converted to JSON-safe dicts before
    serialization so they survive the redis round-trip intact.
    """
    try:
        r = _client()
        safe = _to_json(value)
        r.setex(key, ttl_seconds, json.dumps(safe, default=str))
    except Exception:
        logger.debug("Cache write failed for key %s", key, exc_info=True)


def cache_delete_pattern(pattern: str) -> None:
    """Delete all keys matching a glob pattern. Best-effort."""
    try:
        r = _client()
        keys = r.keys(f"{CACHE_PREFIX}{pattern}")
        if keys:
            r.delete(*keys)
            logger.debug("Cache invalidated %d keys matching %s", len(keys), pattern)
    except Exception:
        logger.debug("Cache invalidation failed for pattern %s", pattern, exc_info=True)


def cache_delete(*keys: str) -> None:
    """Delete specific cache keys. Best-effort."""
    try:
        r = _client()
        full_keys = [f"{CACHE_PREFIX}{k}" for k in keys]
        r.delete(*full_keys)
    except Exception:
        logger.debug("Cache delete failed for keys %s", keys, exc_info=True)
