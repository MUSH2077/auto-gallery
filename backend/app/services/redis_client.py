"""Shared Redis connection factory.

All modules should use ``get_redis()`` instead of creating their own
``redis.from_url(settings.redis_url)`` connections.  This gives a single
place to configure connection pooling, timeouts, and retries.
"""

from __future__ import annotations

import redis as redis_lib

from app.config import settings

_client: redis_lib.Redis | None = None


def get_redis() -> redis_lib.Redis:
    """Return a shared Redis client, creating one lazily on first call.

    The client is cached at module level.  In the rare case a caller needs
    a completely fresh connection (e.g. after a fork), call
    ``reset_redis()`` first.
    """
    global _client
    if _client is None:
        _client = redis_lib.from_url(settings.redis_url, decode_responses=False)
    return _client


def reset_redis() -> None:
    """Close and discard the cached Redis client.

    Useful after process forks or when connection parameters change at runtime.
    """
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
