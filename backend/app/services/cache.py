"""Redis-based API response cache with TTL and pattern-based invalidation."""

import json
import logging
from typing import Any
from uuid import uuid4

from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

CACHE_PREFIX = "cache:api:"
GENERATION_PREFIX = "cache:generation:"
LOCK_PREFIX = "cache:lock:"
DEFAULT_TTL = 300  # 5 minutes
INTERACTIVE_LIST_TTL = 30
STATS_TTL = 60

# ── Per-endpoint TTL registry (single source of truth) ──
TTL = {
    "gitllery:status": 30,
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


def cache_get_with_status(key: str) -> tuple[bool, Any | None]:
    """Read a value while preserving cache-miss vs Redis-outage semantics.

    Most callers can treat both outcomes as a miss and should keep using
    :func:`cache_get`.  Cross-process single-flight code cannot: a contended
    lease must wait for its winner, whereas an unavailable Redis must fall
    back to the database under the process-local lock.
    """

    try:
        raw = _client().get(key)
        if raw is None:
            return True, None
        return True, json.loads(raw)
    except Exception:
        logger.debug("Cache status read failed for key %s", key, exc_info=True)
        return False, None


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


def cache_generation(domain: str) -> int:
    """Return the monotonic invalidation generation for one cache domain.

    Generation keys let high-cardinality caches become unreachable in O(1)
    without blocking Redis with ``KEYS``.  Missing Redis is deliberately
    equivalent to generation zero; callers still retain database correctness.
    """

    try:
        raw = _client().get(f"{GENERATION_PREFIX}{domain}")
        return int(raw or 0)
    except Exception:
        logger.debug("Cache generation read failed for %s", domain, exc_info=True)
        return 0


def cache_bump_generation(domain: str) -> int:
    """Invalidate a domain in O(1) and return its new generation."""

    try:
        return int(_client().incr(f"{GENERATION_PREFIX}{domain}"))
    except Exception:
        logger.debug("Cache generation bump failed for %s", domain, exc_info=True)
        return 0


def cache_try_lock(name: str, ttl_seconds: int = 30) -> str | None:
    """Acquire a small Redis lease and return its ownership token.

    The lease is an optimization, never a source of correctness.  A caller
    receiving ``None`` may wait for the winner or safely fall back to its local
    single-flight guard when Redis is unavailable.
    """

    token = uuid4().hex
    try:
        acquired = _client().set(
            f"{LOCK_PREFIX}{name}",
            token,
            nx=True,
            ex=max(1, int(ttl_seconds)),
        )
        return token if acquired else None
    except Exception:
        logger.debug("Cache lock acquisition failed for %s", name, exc_info=True)
        return None


def cache_refresh_lock(name: str, token: str, ttl_seconds: int = 30) -> bool:
    """Renew a lease only when ``token`` still owns it."""

    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('expire', KEYS[1], ARGV[2])
    end
    return 0
    """
    try:
        return bool(_client().eval(
            script,
            1,
            f"{LOCK_PREFIX}{name}",
            token,
            max(1, int(ttl_seconds)),
        ))
    except Exception:
        logger.debug("Cache lock renewal failed for %s", name, exc_info=True)
        return False


def cache_release_lock(name: str, token: str) -> bool:
    """Release a lease without deleting a newer owner's lock."""

    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        return bool(_client().eval(
            script,
            1,
            f"{LOCK_PREFIX}{name}",
            token,
        ))
    except Exception:
        logger.debug("Cache lock release failed for %s", name, exc_info=True)
        return False


def cache_delete_pattern(pattern: str) -> int:
    """Delete matching keys incrementally. Best-effort.

    Exact ``<domain>:*`` invalidations also advance that domain's generation,
    so latency-sensitive count caches do not depend on the scan completing.
    ``scan_iter`` avoids the server-wide pause caused by Redis ``KEYS``.
    """
    try:
        r = _client()
        domain, separator, suffix = pattern.partition(":")
        if separator and suffix == "*" and domain:
            r.incr(f"{GENERATION_PREFIX}{domain}")

        deleted = 0
        batch: list[Any] = []
        for key in r.scan_iter(match=f"{CACHE_PREFIX}{pattern}", count=100):
            batch.append(key)
            if len(batch) >= 100:
                deleted += int(r.delete(*batch) or 0)
                batch.clear()
        if batch:
            deleted += int(r.delete(*batch) or 0)
        logger.debug("Cache invalidated %d keys matching %s", deleted, pattern)
        return deleted
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
