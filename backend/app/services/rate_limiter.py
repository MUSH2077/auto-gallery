"""Simple Redis-backed rate limiter for API endpoints.

Usage::

    from app.services.rate_limiter import RateLimiter
    limiter = RateLimiter(prefix="login", max_requests=5, window_seconds=60)
    ok, remaining, retry_after = await limiter.check(client_ip)
    if not ok:
        raise HTTPException(429, detail="Too many requests")
"""

from __future__ import annotations

from app.services.redis_client import get_redis
from app.services.redis_budget import http_redis_call


class RateLimiter:
    """Token-bucket style rate limiter using a Redis string key with TTL."""

    def __init__(
        self,
        prefix: str = "rate_limit",
        max_requests: int = 5,
        window_seconds: int = 60,
    ) -> None:
        self._prefix = prefix
        self._max = max_requests
        self._window = window_seconds

    def _key(self, identifier: str) -> str:
        return f"rate_limit:{self._prefix}:{identifier}"

    async def check(self, identifier: str) -> tuple[bool, int, int]:
        """Check whether *identifier* is within the rate limit.

        Returns ``(allowed, remaining, retry_after_seconds)``.
        """
        try:
            return await http_redis_call(self._check, identifier)
        except Exception:
            return True, self._max, 0

    def _check(self, identifier: str) -> tuple[bool, int, int]:
        r = get_redis()
        key = self._key(identifier)

        try:
            # A timed-out response must not leave an increment without expiry.
            current, ttl = r.eval(
                "local n = redis.call('incr', KEYS[1]); "
                "if n == 1 then redis.call('expire', KEYS[1], ARGV[1]); end; "
                "return {n, redis.call('ttl', KEYS[1])}",
                1, key, self._window,
            )

            remaining = max(0, self._max - current)
            ttl = ttl or self._window
            retry_after = max(0, ttl)
            return current <= self._max, remaining, retry_after
        except Exception:
            # If Redis is down, fail open — don't lock users out.
            return True, self._max, 0
