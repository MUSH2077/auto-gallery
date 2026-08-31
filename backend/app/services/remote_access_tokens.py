"""Opaque, short-lived tickets for private remote discovery operations."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


REMOTE_ACCESS_TOKEN_TTL_SECONDS = 10 * 60
RemoteMediaVariant = Literal["avatar", "thumbnail", "preview"]


class RemoteAccessTokenError(ValueError):
    """An opaque remote access ticket is invalid or expired."""


def validate_pixiv_media_url(url: str) -> str:
    """Accept only the exact HTTPS Pixiv image origin."""

    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "i.pximg.net"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise ValueError("Pixiv media URL is not allowed")
    return url


class RemoteAccessTokenService:
    """Encrypt and authenticate provider URLs and action provenance."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        raw_secret = (secret or settings.secret_key).encode("utf-8")
        self._fernet = Fernet(base64.urlsafe_b64encode(sha256(raw_secret).digest()))
        self._clock = clock

    def _issue(self, purpose: str, payload: Mapping[str, Any]) -> str:
        body = json.dumps(
            {"v": 1, "purpose": purpose, **dict(payload)},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._fernet.encrypt_at_time(body, int(self._clock())).decode("ascii")

    def _verify(self, token: str, purpose: str) -> dict[str, Any]:
        try:
            body = self._fernet.decrypt_at_time(
                token.encode("ascii"),
                ttl=REMOTE_ACCESS_TOKEN_TTL_SECONDS,
                current_time=int(self._clock()),
            )
        except InvalidToken as exc:
            # Fernet deliberately does not reveal whether authentication or
            # time validation failed. A timestamp check lets callers expose a
            # stable typed error without disclosing ticket contents.
            try:
                issued_at = self._fernet.extract_timestamp(token.encode("ascii"))
            except (InvalidToken, UnicodeEncodeError):
                raise RemoteAccessTokenError("remote access token is invalid") from exc
            if int(self._clock()) - issued_at > REMOTE_ACCESS_TOKEN_TTL_SECONDS:
                raise RemoteAccessTokenError("remote access token is expired") from exc
            raise RemoteAccessTokenError("remote access token is invalid") from exc
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise RemoteAccessTokenError("remote access token is invalid") from exc
        if not isinstance(payload, dict) or payload.pop("v", None) != 1:
            raise RemoteAccessTokenError("remote access token is invalid")
        if payload.pop("purpose", None) != purpose:
            raise RemoteAccessTokenError("remote access token is invalid")
        return payload

    def issue_media(
        self,
        *,
        user_id: int,
        candidate_id: UUID,
        remote_account_id: UUID,
        credential_generation: int,
        upstream_url: str,
        variant: RemoteMediaVariant,
    ) -> str:
        if variant not in {"avatar", "thumbnail", "preview"}:
            raise ValueError("Unsupported remote media variant")
        validate_pixiv_media_url(upstream_url)
        return self._issue(
            "media",
            {
                "user_id": user_id,
                "candidate_id": str(candidate_id),
                "remote_account_id": str(remote_account_id),
                "credential_generation": credential_generation,
                "upstream_url": upstream_url,
                "variant": variant,
            },
        )

    def verify_media(self, token: str) -> dict[str, Any]:
        payload = self._verify(token, "media")
        try:
            validate_pixiv_media_url(str(payload["upstream_url"]))
            if payload["variant"] not in {"avatar", "thumbnail", "preview"}:
                raise ValueError
            if int(payload["user_id"]) < 1 or int(payload["credential_generation"]) < 1:
                raise ValueError
            UUID(str(payload["candidate_id"]))
            UUID(str(payload["remote_account_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteAccessTokenError("remote access token is invalid") from exc
        return payload

    def issue_cursor(self, payload: Mapping[str, Any]) -> str:
        return self._issue("cursor", payload)

    def verify_cursor(self, token: str) -> dict[str, Any]:
        return self._verify(token, "cursor")

    def issue_work(self, payload: Mapping[str, Any]) -> str:
        return self._issue("work", payload)

    def verify_work(self, token: str) -> dict[str, Any]:
        return self._verify(token, "work")
