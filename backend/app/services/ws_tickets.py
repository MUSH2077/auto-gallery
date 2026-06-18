"""Short-lived one-time tickets for WebSocket authentication."""

from __future__ import annotations

import secrets

from app.services.redis_client import get_redis

TICKET_TTL_SECONDS = 30
_KEY_PREFIX = "ws:ticket:"


def issue_ws_ticket(username: str) -> tuple[str, int]:
    """Create a short-lived ticket that can authenticate one WebSocket handshake."""
    ticket = secrets.token_urlsafe(32)
    redis = get_redis()
    redis.setex(f"{_KEY_PREFIX}{ticket}", TICKET_TTL_SECONDS, username.encode("utf-8"))
    return ticket, TICKET_TTL_SECONDS


def consume_ws_ticket(ticket: str) -> str | None:
    """Consume a ticket exactly once and return its username if valid."""
    if not ticket:
        return None

    redis = get_redis()
    key = f"{_KEY_PREFIX}{ticket}"
    value = redis.getdel(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
