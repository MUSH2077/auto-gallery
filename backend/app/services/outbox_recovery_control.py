"""Single scheduler-supervisor consumer of resource recovery notifications."""
from __future__ import annotations

import logging
import time

import redis

from app.services.resource_pressure_snapshot import RESOURCE_CONTROL_CHANNEL

logger = logging.getLogger(__name__)


class OutboxRecoveryControl:
    def __init__(self, redis_url: str):
        self.client = redis.Redis.from_url(redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
        self.pubsub = None
        self.pending = False
        self.next_check_at = 0.0

    def close(self):
        if self.pubsub is not None:
            self.pubsub.close()
            self.pubsub = None
        self.client.close()

    def poll(self):
        from app.services.outbox_coordinator import wake_resource_deferred_outboxes

        if self.pubsub is None and time.monotonic() < self.next_check_at:
            return
        try:
            if self.pubsub is None:
                self.pubsub = self.client.pubsub(ignore_subscribe_messages=True)
                self.pubsub.subscribe(RESOURCE_CONTROL_CHANNEL)
                # Reconnection/startup must reconsider existing wait flags.
                self.pending = True
            for _ in range(32):
                message = self.pubsub.get_message(timeout=0)
                if message is None:
                    break
                if message.get("type") == "message":
                    self.pending = True
            now = time.monotonic()
            if not self.pending or now < self.next_check_at:
                return
            self.pending = False
            result = wake_resource_deferred_outboxes()
            self.next_check_at = time.monotonic() + (5.0 if result.get("deferred") else 1.0)
            if result.get("deferred"):
                self.pending = True
        except redis.RedisError:
            if self.pubsub is not None:
                try:
                    self.pubsub.close()
                except redis.RedisError:
                    pass
                self.pubsub = None
            self.pending = True
            self.next_check_at = time.monotonic() + 5.0
        except Exception:
            logger.warning("Resource recovery event check failed; timer fallback retained", exc_info=True)
            self.pending = True
            self.next_check_at = time.monotonic() + 5.0
