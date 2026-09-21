"""Bounded Pixiv image retrieval for the signed remote media endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from app.services.remote_access_tokens import validate_pixiv_media_url


PIXIV_REFERER = "https://www.pixiv.net/"
SMALL_MEDIA_LIMIT = 5 * 1024 * 1024
PREVIEW_MEDIA_LIMIT = 25 * 1024 * 1024


class RemoteMediaError(RuntimeError):
    """The upstream media response failed the proxy security contract."""


@dataclass(frozen=True)
class RemoteMediaResult:
    content: bytes
    content_type: str


async def fetch_pixiv_media(
    upstream_url: str,
    *,
    variant: Literal["avatar", "header", "thumbnail", "preview"],
    client: httpx.AsyncClient | None = None,
) -> RemoteMediaResult:
    validate_pixiv_media_url(upstream_url)
    if variant not in {"avatar", "header", "thumbnail", "preview"}:
        raise ValueError("Unsupported remote media variant")
    size_limit = PREVIEW_MEDIA_LIMIT if variant == "preview" else SMALL_MEDIA_LIMIT
    owns_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=5.0),
        follow_redirects=False,
    )
    try:
        async with active_client.stream(
            "GET",
            upstream_url,
            headers={"Referer": PIXIV_REFERER, "Accept": "image/*"},
            follow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise RemoteMediaError("Pixiv media redirect is not allowed")
            if response.status_code != 200:
                raise RemoteMediaError("Pixiv media request failed")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if not content_type.startswith("image/"):
                raise RemoteMediaError("Pixiv media response has an invalid image MIME type")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > size_limit:
                        raise RemoteMediaError("Pixiv media response exceeds the size limit")
                except ValueError as exc:
                    raise RemoteMediaError("Pixiv media response has an invalid size") from exc
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > size_limit:
                    raise RemoteMediaError("Pixiv media response exceeds the size limit")
                chunks.append(chunk)
            return RemoteMediaResult(content=b"".join(chunks), content_type=content_type)
    except httpx.HTTPError as exc:
        raise RemoteMediaError("Pixiv media request failed") from exc
    finally:
        if owns_client:
            await active_client.aclose()
