from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

import redis as redis_lib

from app.config import settings

logger = logging.getLogger(__name__)

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0
"""


@contextmanager
def redis_lock(name: str, ttl_seconds: int = 60):
    token = uuid.uuid4().hex
    client = redis_lib.from_url(settings.redis_url)
    acquired = False
    try:
        acquired = bool(client.set(name, token, nx=True, ex=ttl_seconds))
        yield acquired
    finally:
        if acquired:
            try:
                client.eval(_RELEASE_SCRIPT, 1, name, token)
            except Exception:
                logger.warning("Failed to release Redis lock %s", name, exc_info=True)
        client.close()
