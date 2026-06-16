"""Redis-based API response cache with TTL and pattern-based invalidation."""

import json
import logging
from typing import Any

from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

CACHE_PREFIX = "cache:api:"
DEFAULT_TTL = 300  # 5 minutes
INTERACTIVE_LIST_TTL = 30
STATS_TTL = 60

# ── Per-endpoint TTL registry (single source of truth) ──
TTL = {
    "creators:list": INTERACTIVE_LIST_TTL,
    "creators:count": INTERACTIVE_LIST_TTL,
    "creators:stats": STATS_TTL,
    "works:list": 300,
    "subscriptions:list": INTERACTIVE_LIST_TTL,
}

# Track first cache failure to avoid log spam (resets on success)
_cache_healthy: bool = True


def _client():
    """Return the shared Redis client (lazy singleton)."""
    return get_redis()


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


def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching a glob pattern. Best-effort."""
    try:
        r = _client()
        keys = r.keys(f"{CACHE_PREFIX}{pattern}")
        if keys:
            r.delete(*keys)
            logger.debug("Cache invalidated %d keys matching %s", len(keys), pattern)
            return len(keys)
        logger.debug("Cache invalidated 0 keys matching %s", pattern)
        return 0
    except Exception:
        logger.debug("Cache invalidation failed for pattern %s", pattern, exc_info=True)
        return 0


def invalidate_api_caches(*domains: str) -> dict[str, int]:
    """Invalidate one or more API cache domains such as creators/subscriptions."""
    deleted: dict[str, int] = {}
    for domain in dict.fromkeys(domains):
        if domain:
            deleted[domain] = cache_delete_pattern(f"{domain}:*")
    if deleted:
        total = sum(deleted.values())
        if total:
            logger.info("API cache invalidated domains=%s deleted=%d", sorted(deleted), total)
        else:
            logger.debug("API cache invalidated domains=%s deleted=0", sorted(deleted))
    return deleted


def invalidate_creator_subscription_caches(*, include_works: bool = False) -> dict[str, int]:
    """Invalidate caches backing creator/subscription aggregate list views."""
    domains = ["creators", "subscriptions"]
    if include_works:
        domains.append("works")
    return invalidate_api_caches(*domains)


def cache_delete(*keys: str) -> None:
    """Delete specific cache keys. Best-effort."""
    try:
        r = _client()
        full_keys = [f"{CACHE_PREFIX}{k}" for k in keys]
        r.delete(*full_keys)
    except Exception:
        logger.debug("Cache delete failed for keys %s", keys, exc_info=True)
