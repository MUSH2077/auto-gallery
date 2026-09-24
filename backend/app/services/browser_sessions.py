"""Revocable browser sessions backed by the existing Redis service."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import RedisError
from fastapi import HTTPException, Request

from app.config import settings

SESSION_COOKIE = "ag_session"
CSRF_COOKIE = "ag_csrf"
CSRF_HEADER = "x-csrf-token"
_KEY_PREFIX = "browser:session:"
_client: aioredis.Redis | None = None


@dataclass(frozen=True, slots=True)
class BrowserSession:
    user_id: int
    csrf: str
    password_fingerprint: str


class SessionStoreUnavailable(RuntimeError):
    pass


def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.Redis.from_url(
            settings.redis_url,
            max_connections=8,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def close_browser_session_store() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _key(token: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(token.encode("ascii")).hexdigest()


def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


def session_lifetime_seconds() -> int:
    return settings.access_token_expire_minutes * 60


async def create_browser_session(user) -> tuple[str, BrowserSession]:
    token = secrets.token_urlsafe(32)
    session = BrowserSession(
        user_id=user.id,
        csrf=secrets.token_urlsafe(32),
        password_fingerprint=password_fingerprint(user.password_hash),
    )
    try:
        created = await _redis().set(
            _key(token),
            json.dumps({
                "user_id": session.user_id,
                "csrf": session.csrf,
                "password_fingerprint": session.password_fingerprint,
            }),
            ex=session_lifetime_seconds(),
            nx=True,
        )
    except RedisError as exc:
        raise SessionStoreUnavailable from exc
    if not created:
        raise SessionStoreUnavailable("session key collision")
    return token, session


async def load_browser_session(token: str) -> BrowserSession | None:
    if not token or len(token) > 128 or not token.isascii():
        return None
    try:
        raw = await _redis().get(_key(token))
    except RedisError as exc:
        raise SessionStoreUnavailable from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return BrowserSession(
            user_id=int(data["user_id"]),
            csrf=str(data["csrf"]),
            password_fingerprint=str(data["password_fingerprint"]),
        )
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return None


async def revoke_browser_session(token: str) -> None:
    if not token or len(token) > 128 or not token.isascii():
        return
    try:
        await _redis().delete(_key(token))
    except RedisError as exc:
        raise SessionStoreUnavailable from exc


def browser_origin(request: Request) -> str:
    origin = request.headers.get("origin")
    allowed = {value.strip() for value in settings.browser_session_origins.split(",") if value.strip()}
    if not origin or origin not in allowed:
        raise HTTPException(status_code=403, detail="Untrusted browser origin")
    return origin


def verify_browser_csrf(request: Request, session: BrowserSession) -> None:
    # The readable cookie gives the browser a header value; the authoritative
    # copy stays in Redis and is bound to this authenticated session.
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not header or not cookie or not secrets.compare_digest(header, session.csrf) or not secrets.compare_digest(cookie, session.csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
