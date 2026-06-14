"""Short-lived signed media URL helpers."""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

from app.config import settings

MEDIA_SIGNED_URL_TTL_SECONDS = 10 * 60
SIGNED_MEDIA_SIZES = {"preview", "original"}


def _message(asset_id: str, size: str, expires: int) -> str:
    return f"{asset_id}:{size}:{expires}"


def sign_media_token(asset_id: str, size: str, expires: int) -> str:
    """Return an HMAC token for a media asset, size, and expiry."""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        _message(asset_id, size, expires).encode("utf-8"),
        sha256,
    ).hexdigest()


def verify_media_token(asset_id: str, size: str, expires: int | str | None, token: str | None) -> bool:
    """Validate a signed media URL token."""
    if size not in SIGNED_MEDIA_SIZES or not expires or not token:
        return False
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_int < int(time.time()):
        return False
    expected = sign_media_token(asset_id, size, expires_int)
    return hmac.compare_digest(expected, token)


def signed_media_url(asset_id: str, size: str, ttl_seconds: int = MEDIA_SIGNED_URL_TTL_SECONDS) -> str:
    """Build a relative signed media URL for preview/original image access."""
    if size not in SIGNED_MEDIA_SIZES:
        raise ValueError(f"Unsupported signed media size: {size}")
    expires = int(time.time()) + ttl_seconds
    token = sign_media_token(asset_id, size, expires)
    return f"/media/{size}/{asset_id}?expires={expires}&token={token}"
