"""Short-lived one-time tickets for WebSocket authentication."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from app.services.redis_client import get_redis

TICKET_TTL_SECONDS = 30
_KEY_PREFIX = "ws:ticket:"
_BROWSER_PREFIX = "\x00ag-browser-v1:"
_BEARER_PREFIX = "\x00ag-bearer-v1:"


@dataclass(frozen=True)
class BrowserTicket:
    username: str
    session_token: str


@dataclass(frozen=True)
class BearerTicket:
    user_id: int
    credential_version: int


def issue_ws_ticket(user, session_token: str | None = None) -> tuple[str, int]:
    """Create a short-lived ticket that can authenticate one WebSocket handshake."""
    ticket = secrets.token_urlsafe(32)
    redis = get_redis()
    value = (
        _BROWSER_PREFIX + json.dumps({"username": user.username, "session_token": session_token})
        if session_token else _BEARER_PREFIX + json.dumps({"user_id": user.id, "credential_version": user.credential_version})
    )
    redis.setex(f"{_KEY_PREFIX}{ticket}", TICKET_TTL_SECONDS, value.encode("utf-8"))
    return ticket, TICKET_TTL_SECONDS


def consume_ws_ticket(ticket: str) -> BearerTicket | BrowserTicket | None:
    """Consume a ticket exactly once; reject former username-only tickets."""
    if not ticket:
        return None

    redis = get_redis()
    key = f"{_KEY_PREFIX}{ticket}"
    value = redis.getdel(key)
    if value is None:
        return None
    decoded = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    if decoded.startswith(_BROWSER_PREFIX):
        try:
            data = json.loads(decoded[len(_BROWSER_PREFIX):])
            username = data["username"]
            session_token = data["session_token"]
            if not isinstance(username, str) or not username or not isinstance(session_token, str) or not session_token:
                return None
            return BrowserTicket(username=username, session_token=session_token)
        except (ValueError, TypeError, KeyError):
            return None
    if decoded.startswith(_BEARER_PREFIX):
        try:
            data = json.loads(decoded[len(_BEARER_PREFIX):])
            user_id, version = data["user_id"], data["credential_version"]
            if type(user_id) is int and user_id > 0 and type(version) is int and version > 0:
                return BearerTicket(user_id, version)
        except (ValueError, TypeError, KeyError):
            pass
    return None
