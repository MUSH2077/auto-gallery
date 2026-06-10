from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""


@asynccontextmanager
async def redis_lock(name: str, ttl_seconds: int = 60):
    """Async Redis distributed lock using asyncio.to_thread.

    Wraps synchronous redis-py operations in a thread so they don't
    block the FastAPI event loop. Safe for use with ``async with``.
    """
    token = uuid.uuid4().hex
    client = get_redis()
    acquired = False
    try:
        acquired = await asyncio.to_thread(
            lambda: bool(client.set(name, token, nx=True, ex=ttl_seconds))
        )
        yield acquired
    finally:
        if acquired:
            try:
                await asyncio.to_thread(
                    lambda: client.eval(_RELEASE_SCRIPT, 1, name, token)
                )
            except Exception:
                logger.warning("Failed to release Redis lock %s", name, exc_info=True)
        await asyncio.to_thread(client.close)
