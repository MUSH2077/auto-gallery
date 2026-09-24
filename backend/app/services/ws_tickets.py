"""Short-lived one-time tickets for WebSocket authentication."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from app.services.redis_client import get_redis

TICKET_TTL_SECONDS = 30
_KEY_PREFIX = "ws:ticket:"


@dataclass(frozen=True)
class BrowserTicket:
    username: str
    session_token: str


def issue_ws_ticket(username: str, session_token: str | None = None) -> tuple[str, int]:
    """Create a short-lived ticket that can authenticate one WebSocket handshake."""
    ticket = secrets.token_urlsafe(32)
    redis = get_redis()
    value = json.dumps({"username": username, "session_token": session_token}) if session_token else username
    redis.setex(f"{_KEY_PREFIX}{ticket}", TICKET_TTL_SECONDS, value.encode("utf-8"))
    return ticket, TICKET_TTL_SECONDS


def consume_ws_ticket(ticket: str) -> str | BrowserTicket | None:
    """Consume a ticket exactly once and return its username if valid."""
    if not ticket:
        return None

    redis = get_redis()
    key = f"{_KEY_PREFIX}{ticket}"
    value = redis.getdel(key)
    if value is None:
        return None
    decoded = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    if decoded.startswith("{"):
        try:
            data = json.loads(decoded)
            return BrowserTicket(username=data["username"], session_token=data["session_token"])
        except (ValueError, TypeError, KeyError):
            return None
    return decoded
