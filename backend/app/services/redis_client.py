"""Shared Redis connection factory.

All modules should use ``get_redis()`` instead of creating their own
``redis.from_url(settings.redis_url)`` connections.  This gives a single
place to configure connection pooling, timeouts, and retries.
"""

from __future__ import annotations

import redis as redis_lib

from app.config import settings

# Memory-optimized: single shared connection pool instead of letting each
# ``redis.from_url()`` call create its own pool (default max_connections=2**31).
# On a 7.5 GB NAS, this prevents runaway connection counts from health checks,
# RQ queues, and ad-hoc connections in download/import jobs.
_shared_pool: redis_lib.ConnectionPool | None = None

_client: redis_lib.Redis | None = None


def _get_pool() -> redis_lib.ConnectionPool:
    """Return a singleton connection pool with conservative limits."""
    global _shared_pool
    if _shared_pool is None:
        _shared_pool = redis_lib.ConnectionPool.from_url(
            settings.redis_url,
            max_connections=20,          # hard cap — sufficient for all RQ workers + API
            socket_keepalive=True,
            socket_connect_timeout=5,
            # Longer than the worker's bounded 10s dequeue, but finite so a
            # black-holed Redis connection cannot stall pressure/lock guards.
            socket_timeout=15,
            retry_on_timeout=True,
            health_check_interval=30,
            decode_responses=False,
        )
    return _shared_pool


def get_redis() -> redis_lib.Redis:
    """Return a shared Redis client, creating one lazily on first call.

    The client is cached at module level and shares a bounded connection pool.
    In the rare case a caller needs a completely fresh connection
    (e.g. after a fork), call ``reset_redis()`` first.
    """
    global _client
    if _client is None:
        _client = redis_lib.Redis(connection_pool=_get_pool())
    return _client


def reset_redis() -> None:
    """Close and discard the cached Redis client and its connection pool.

    Useful after process forks or when connection parameters change at runtime.
    """
    global _client, _shared_pool
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
    if _shared_pool is not None:
        try:
            _shared_pool.disconnect()
        except Exception:
            pass
        _shared_pool = None
