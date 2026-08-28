"""Short-lived X OAuth 2.0 PKCE state; access tokens never enter Redis."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from app.remote_discovery.common import HttpxRemoteTransport, checked_payload


STATE_PREFIX = "remote-discovery:x:oauth-state:"
STATE_TTL_SECONDS = 600
X_SCOPES = ("users.read", "follows.read", "list.read", "offline.access")


def validate_x_oauth_scopes(raw_scopes: str) -> list[str]:
    """Require the complete discovery grant before persisting any OAuth tokens."""
    scopes = list(dict.fromkeys(raw_scopes.split()))
    missing = [scope for scope in X_SCOPES if scope not in scopes]
    if missing:
        raise ValueError(f"X OAuth response is missing required scopes: {', '.join(missing)}")
    return scopes


@dataclass(frozen=True)
class OAuthAuthorization:
    url: str
    state: str


@dataclass(frozen=True)
class OAuthStatePayload:
    verifier: str
    user_id: int
    account_id: str | None = None


class XOAuthPKCEState:
    def __init__(self, redis_client, *, client_id: str, redirect_uri: str):
        if not client_id or not redirect_uri:
            raise ValueError("X OAuth client_id and redirect_uri are required")
        self.redis = redis_client
        self.client_id = client_id
        self.redirect_uri = redirect_uri

    @staticmethod
    def _key(state: str) -> str:
        return f"{STATE_PREFIX}{state}"

    def authorize(self, *, user_id: int, account_id: str | None = None) -> OAuthAuthorization:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        payload = json.dumps(
            {"user_id": user_id, "verifier": verifier, "account_id": account_id},
            separators=(",", ":"),
        )
        self.redis.setex(self._key(state), STATE_TTL_SECONDS, payload)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(X_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return OAuthAuthorization(
            url=f"https://x.com/i/oauth2/authorize?{query}",
            state=state,
        )

    def consume(self, *, state: str, user_id: int) -> OAuthStatePayload:
        key = self._key(state)
        raw = None
        if hasattr(self.redis, "eval"):
            # Read, verify ownership, and consume in one Redis operation. A
            # cross-user callback leaves both the value and its original TTL
            # untouched.
            result = self.redis.eval(
                """
                local raw = redis.call('GET', KEYS[1])
                if not raw then return {0, ''} end
                local ok, payload = pcall(cjson.decode, raw)
                if not ok then
                    redis.call('DEL', KEYS[1])
                    return {0, ''}
                end
                if tostring(payload.user_id) ~= ARGV[1] then return {2, raw} end
                redis.call('DEL', KEYS[1])
                return {1, raw}
                """,
                1,
                key,
                str(user_id),
            )
            status, raw = int(result[0]), result[1]
            if status == 2:
                raise ValueError("OAuth state owner does not match the current user")
            if status == 0:
                raw = None
        else:
            # Lightweight test doubles may not implement Lua. Verify before
            # GETDEL so the ownership-failure path still preserves the TTL.
            candidate = self.redis.get(key)
            if candidate is not None:
                parsed_candidate = self._parse(candidate)
                if parsed_candidate.user_id != user_id:
                    raise ValueError("OAuth state owner does not match the current user")
                raw = self.redis.getdel(key)
        if raw is None:
            raise ValueError("OAuth state expired or already used")
        payload = self._parse(raw)
        if payload.user_id != user_id:
            raise ValueError("OAuth state owner does not match the current user")
        return payload

    @staticmethod
    def _parse(raw) -> OAuthStatePayload:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            parsed = json.loads(raw)
            return OAuthStatePayload(
                verifier=str(parsed["verifier"]),
                user_id=int(parsed["user_id"]),
                account_id=(str(parsed["account_id"]) if parsed.get("account_id") else None),
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("OAuth state expired or already used") from exc


class XOAuthExchange:
    """Injected authorization-code exchange boundary."""

    TOKEN_URL = "https://api.x.com/2/oauth2/token"

    def __init__(self, transport=None):
        self.transport = transport or HttpxRemoteTransport()

    async def exchange(self, *, code: str, verifier: str, redirect_uri: str, client_id: str):
        response = await self.transport.request(
            "POST",
            self.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        payload = checked_payload(response, provider="X OAuth")
        if not payload.get("access_token") and not payload.get("refresh_token"):
            raise ValueError("X OAuth response did not include a usable token")
        return dict(payload)


def get_x_oauth_exchange() -> XOAuthExchange:
    return XOAuthExchange()
