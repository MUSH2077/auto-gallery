"""Authenticated storage and short-lived use of remote account credentials."""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from copy import deepcopy
from pathlib import PurePath
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialKeyError(ValueError):
    """The separately managed remote credential key is unusable."""


class CredentialDecryptionError(ValueError):
    """Credential ciphertext could not be authenticated or decoded."""


def decode_remote_credential_key(value: str | bytes) -> bytes:
    """Decode a URL-safe base64 AES-256 key with an actionable error."""

    if isinstance(value, bytes):
        encoded = value.strip()
    else:
        encoded = value.strip().encode("ascii", "ignore")
    if not encoded or encoded.lower().startswith(b"change-me"):
        raise CredentialKeyError(
            "REMOTE_CREDENTIAL_KEY must be a separately generated URL-safe base64 AES-256 key"
        )
    try:
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialKeyError(
            "REMOTE_CREDENTIAL_KEY must be valid URL-safe base64 encoding exactly 32 bytes"
        ) from exc
    if len(decoded) != 32:
        raise CredentialKeyError(
            "REMOTE_CREDENTIAL_KEY must decode to exactly 32 bytes for AES-256-GCM"
        )
    return decoded


def validate_configured_remote_credential_key(value: str | None) -> None:
    """Validate a configured key while allowing deployments not using discovery yet."""

    if value:
        decode_remote_credential_key(value)


def _aad(*, user_id: int, source: str, account_id: UUID | str) -> bytes:
    if user_id < 1 or not source.strip() or not str(account_id).strip():
        raise ValueError("credential identity requires user_id, source, and account_id")
    # Length-prefixed JSON is unambiguous and binds all three ownership fields.
    return json.dumps(
        {
            "account_id": str(account_id),
            "source": source,
            "user_id": user_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class RedactedCredentials(Mapping[str, Any]):
    """Credential mapping whose routine text representations never expose values."""

    def __init__(self, values: Mapping[str, Any]):
        self.__values = deepcopy(dict(values))

    def __getitem__(self, key: str) -> Any:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __repr__(self) -> str:
        keys = ", ".join(sorted(self.__values))
        return f"RedactedCredentials(keys=[{keys}], values=<redacted>)"

    __str__ = __repr__

    def materialize(self) -> dict[str, Any]:
        """Return an ephemeral copy for an authenticated transport boundary."""

        return deepcopy(self.__values)

    def _replace_values(self, values: Mapping[str, Any]) -> None:
        """Replace ephemeral values without ever exposing them in repr/str."""

        self.__values = deepcopy(dict(values))


class RefreshableCredentials(RedactedCredentials):
    """Ephemeral credentials with a storage-owned OAuth rotation coordinator."""

    def __init__(
        self,
        values: Mapping[str, Any],
        rotate: Callable[
            [Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]],
            Awaitable[Mapping[str, Any]],
        ],
    ):
        super().__init__(values)
        self.__rotate = rotate

    async def rotate_oauth_tokens(
        self,
        request_rotation: Callable[
            [Mapping[str, Any]], Awaitable[Mapping[str, Any]]
        ],
    ) -> dict[str, Any]:
        rotated = dict(await self.__rotate(request_rotation))
        self._replace_values(rotated)
        return deepcopy(rotated)


def _walk_values(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_values(child, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _walk_values(child, (*path, str(index)))
    else:
        yield path, value


class DownloadAuthenticationOverride(Mapping[str, Any]):
    """Ephemeral authentication config which cannot reference durable secret files."""

    def __init__(self, values: Mapping[str, Any]):
        copied = deepcopy(dict(values))
        for path, value in _walk_values(copied):
            if not isinstance(value, str):
                continue
            credential_field = any(
                token.casefold() in {"cookie", "cookies", "token", "refresh-token", "refresh_token"}
                for token in path
            )
            if credential_field and (
                value.startswith("file:") or PurePath(value).is_absolute()
            ):
                raise ValueError("download authentication override cannot contain a durable credential path")
        self.__values = copied

    def __getitem__(self, key: str) -> Any:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __repr__(self) -> str:
        return "DownloadAuthenticationOverride(<redacted>)"

    __str__ = __repr__

    def materialize(self) -> dict[str, Any]:
        return deepcopy(self.__values)


class CredentialVault:
    """AES-256-GCM vault with account ownership bound as authenticated data."""

    VERSION = "v1"
    NONCE_BYTES = 12

    def __init__(self, key: str | bytes):
        self.__aesgcm = AESGCM(decode_remote_credential_key(key))

    def __repr__(self) -> str:
        return "CredentialVault(key=<redacted>, algorithm='AES-256-GCM')"

    def encrypt(
        self,
        values: Mapping[str, Any],
        *,
        user_id: int,
        source: str,
        account_id: UUID | str,
    ) -> str:
        if not isinstance(values, Mapping) or not values:
            raise ValueError("credential payload must be a non-empty mapping")
        plaintext = json.dumps(dict(values), separators=(",", ":"), sort_keys=True).encode("utf-8")
        nonce = os.urandom(self.NONCE_BYTES)
        ciphertext = self.__aesgcm.encrypt(
            nonce,
            plaintext,
            _aad(user_id=user_id, source=source, account_id=account_id),
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{self.VERSION}:{encoded}"

    def decrypt(
        self,
        value: str,
        *,
        user_id: int,
        source: str,
        account_id: UUID | str,
    ) -> RedactedCredentials:
        try:
            version, encoded = value.split(":", 1)
            if version != self.VERSION:
                raise ValueError("unsupported credential ciphertext version")
            payload = base64.b64decode(
                encoded.encode("ascii"), altchars=b"-_", validate=True
            )
            if len(payload) <= self.NONCE_BYTES + 16:
                raise ValueError("credential ciphertext is truncated")
            plaintext = self.__aesgcm.decrypt(
                payload[: self.NONCE_BYTES],
                payload[self.NONCE_BYTES :],
                _aad(user_id=user_id, source=source, account_id=account_id),
            )
            decoded = json.loads(plaintext)
            if not isinstance(decoded, dict) or not decoded:
                raise ValueError("credential plaintext is not a non-empty object")
            return RedactedCredentials(decoded)
        except (InvalidTag, ValueError, TypeError, KeyError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise CredentialDecryptionError(
                "remote credentials could not be authenticated; check account ownership and REMOTE_CREDENTIAL_KEY"
            ) from exc
