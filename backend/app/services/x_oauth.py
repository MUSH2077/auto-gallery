"""Short-lived X OAuth 2.0 PKCE state; access tokens never enter Redis."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings
from app.remote_discovery.common import HttpxRemoteTransport, checked_payload
from app.services.remote_credentials import decode_remote_credential_key


STATE_PREFIX = "remote-discovery:x:oauth-state:"
STATE_TTL_SECONDS = 600
X_SCOPES = (
    "tweet.read",
    "users.read",
    "follows.read",
    "list.read",
    "offline.access",
)
_STATE_VERSION = 1
_STATE_NONCE_BYTES = 12
_STATE_SOURCE = "x"
_STATE_PURPOSE = "auto-gallery:x-oauth-pkce-state:v1"
_STATE_KDF_SALT = b"auto-gallery:remote-credential-subkeys:v1"


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
    def __init__(
        self,
        redis_client,
        *,
        client_id: str,
        redirect_uri: str,
        credential_key: str | bytes | None = None,
    ):
        if not client_id or not redirect_uri:
            raise ValueError("X OAuth client_id and redirect_uri are required")
        self.redis = redis_client
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        root_key = decode_remote_credential_key(
            settings.remote_credential_key if credential_key is None else credential_key
        )
        state_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_STATE_KDF_SALT,
            info=_STATE_PURPOSE.encode("ascii"),
        ).derive(root_key)
        self.__aead = AESGCM(state_key)

    def __repr__(self) -> str:
        return "XOAuthPKCEState(key=<redacted>, algorithm='AES-256-GCM')"

    @staticmethod
    def _key(state: str) -> str:
        return f"{STATE_PREFIX}{state}"

    @staticmethod
    def _account_target(account_id: str | None) -> str:
        return account_id or "new-account"

    @staticmethod
    def _aad(
        *,
        state: str,
        user_id: int,
        account_target: str,
    ) -> bytes:
        return json.dumps(
            {
                "account_target": account_target,
                "purpose": _STATE_PURPOSE,
                "source": _STATE_SOURCE,
                "state": state,
                "user_id": user_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _raw_text(raw) -> str:
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        if isinstance(raw, str):
            return raw
        raise ValueError("OAuth state could not be authenticated")

    def _encrypt_payload(
        self,
        *,
        state: str,
        verifier: str,
        user_id: int,
        account_id: str | None,
    ) -> str:
        account_target = self._account_target(account_id)
        plaintext = json.dumps(
            {
                "account_id": account_id,
                "user_id": user_id,
                "verifier": verifier,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        nonce = os.urandom(_STATE_NONCE_BYTES)
        ciphertext = self.__aead.encrypt(
            nonce,
            plaintext,
            self._aad(
                state=state,
                user_id=user_id,
                account_target=account_target,
            ),
        )
        return json.dumps(
            {
                "account_target": account_target,
                "ciphertext": base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii"),
                "owner_user_id": user_id,
                "version": _STATE_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _decrypt_payload(
        self,
        raw,
        *,
        state: str,
        user_id: int,
    ) -> OAuthStatePayload:
        try:
            envelope = json.loads(self._raw_text(raw))
            if (
                not isinstance(envelope, dict)
                or envelope.get("version") != _STATE_VERSION
                or not isinstance(envelope.get("account_target"), str)
                or not isinstance(envelope.get("owner_user_id"), int)
                or not isinstance(envelope.get("ciphertext"), str)
            ):
                raise ValueError("invalid OAuth state envelope")
            if envelope["owner_user_id"] != user_id:
                raise PermissionError("OAuth state owner does not match the current user")
            encoded = base64.b64decode(
                envelope["ciphertext"].encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            if len(encoded) <= _STATE_NONCE_BYTES + 16:
                raise ValueError("truncated OAuth state ciphertext")
            plaintext = self.__aead.decrypt(
                encoded[:_STATE_NONCE_BYTES],
                encoded[_STATE_NONCE_BYTES:],
                self._aad(
                    state=state,
                    user_id=user_id,
                    account_target=envelope["account_target"],
                ),
            )
            payload = json.loads(plaintext)
            if not isinstance(payload, dict):
                raise ValueError("invalid OAuth state payload")
            account_id = payload.get("account_id")
            if (
                payload.get("user_id") != user_id
                or not isinstance(payload.get("verifier"), str)
                or not payload["verifier"]
                or (account_id is not None and not isinstance(account_id, str))
                or self._account_target(account_id) != envelope["account_target"]
            ):
                raise ValueError("invalid OAuth state payload")
            return OAuthStatePayload(
                verifier=payload["verifier"],
                user_id=user_id,
                account_id=account_id,
            )
        except PermissionError as exc:
            raise ValueError("OAuth state owner does not match the current user") from exc
        except (
            InvalidTag,
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("OAuth state could not be authenticated") from exc

    def authorize(self, *, user_id: int, account_id: str | None = None) -> OAuthAuthorization:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        payload = self._encrypt_payload(
            state=state,
            verifier=verifier,
            user_id=user_id,
            account_id=account_id,
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
        raw = self.redis.get(key)
        if raw is None:
            raise ValueError("OAuth state expired or already used")
        payload = self._decrypt_payload(raw, state=state, user_id=user_id)
        if hasattr(self.redis, "eval"):
            # Delete only the exact ciphertext authenticated above. Concurrent
            # consumers race at this compare-and-delete, so exactly one wins.
            consumed = self.redis.eval(
                """
                local current = redis.call('GET', KEYS[1])
                if not current or current ~= ARGV[1] then return 0 end
                redis.call('DEL', KEYS[1])
                return 1
                """,
                1,
                key,
                raw,
            )
        else:
            current = self.redis.get(key)
            consumed_raw = self.redis.getdel(key) if current == raw else None
            consumed = int(consumed_raw == raw)
        if int(consumed) != 1:
            raise ValueError("OAuth state expired or already used")
        return payload


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
