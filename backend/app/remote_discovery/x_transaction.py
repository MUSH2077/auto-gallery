"""Lazy X web-client transaction IDs backed by pinned gallery-dl."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.remote_discovery.common import MalformedRemoteResponse, RemoteDiscoveryError


class XTransactionIdProvider(Protocol):
    async def generate(self, method: str, url: str) -> str: ...


TextFetcher = Callable[[str], Awaitable[str]]


async def _fetch_current_text(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url)
    if not 200 <= response.status_code < 300:
        raise RemoteDiscoveryError(
            f"X transaction protocol request failed with HTTP {response.status_code}"
        )
    return response.text


def _ondemand_url(homepage: str) -> str:
    from gallery_dl import text

    ondemand_pos = homepage.find('"ondemand.s"')
    if ondemand_pos < 0:
        raise MalformedRemoteResponse("X homepage is missing ondemand transaction state")
    ondemand_key = text.rextr(homepage, ",", ":", ondemand_pos)
    ondemand_s = text.extract(
        homepage,
        ondemand_key + ':"',
        '"',
        ondemand_pos,
    )[0]
    if not ondemand_s:
        raise MalformedRemoteResponse("X homepage has malformed ondemand transaction state")
    return (
        "https://abs.twimg.com/responsive-web/client-web/"
        f"ondemand.s.{ondemand_s}a.js"
    )


class _PrefetchedGalleryDlExtractor:
    """Small gallery-dl boundary containing only already-fetched public state."""

    log = logging.getLogger(__name__)

    def __init__(self, *, ondemand_url: str, ondemand_javascript: str):
        self.ondemand_url = ondemand_url
        self.ondemand_javascript = ondemand_javascript

    def request(self, url: str) -> SimpleNamespace:
        if url != self.ondemand_url:
            raise MalformedRemoteResponse("gallery-dl requested unexpected X transaction state")
        return SimpleNamespace(text=self.ondemand_javascript)

    @staticmethod
    def cache(func, *args, **_kwargs):
        # auto-gallery owns the bounded in-memory cache around the fully
        # initialized object; gallery-dl receives no durable cache here.
        return func(*args)


def _initialize_gallery_dl_transaction(
    homepage: str,
    ondemand_url: str,
    ondemand_javascript: str,
):
    from gallery_dl.extractor.utils.twitter_transaction_id import ClientTransaction

    transaction = ClientTransaction()
    extractor = _PrefetchedGalleryDlExtractor(
        ondemand_url=ondemand_url,
        ondemand_javascript=ondemand_javascript,
    )
    try:
        transaction.initialize(extractor, homepage=homepage)
    except RemoteDiscoveryError:
        raise
    except Exception as exc:
        raise MalformedRemoteResponse(
            "gallery-dl could not initialize current X transaction state"
        ) from exc
    return transaction


class GalleryDlXTransactionIdProvider:
    """Generate authenticated X request IDs from current, short-lived web state."""

    DEFAULT_CACHE_TTL_SECONDS = 10_800

    def __init__(
        self,
        *,
        fetch_text: TextFetcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        if cache_ttl_seconds < 1:
            raise ValueError("X transaction state cache TTL must be positive")
        self._fetch_text = fetch_text or _fetch_current_text
        self._monotonic = monotonic
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_transaction: Any | None = None
        self._expires_at = 0.0
        self._initialize_lock = asyncio.Lock()

    async def _current_transaction(self):
        now = self._monotonic()
        if self._cached_transaction is not None and now < self._expires_at:
            return self._cached_transaction
        async with self._initialize_lock:
            now = self._monotonic()
            if self._cached_transaction is not None and now < self._expires_at:
                return self._cached_transaction
            homepage = await self._fetch_text("https://x.com/")
            ondemand_url = await asyncio.to_thread(_ondemand_url, homepage)
            ondemand_javascript = await self._fetch_text(ondemand_url)
            transaction = await asyncio.to_thread(
                _initialize_gallery_dl_transaction,
                homepage,
                ondemand_url,
                ondemand_javascript,
            )
            self._cached_transaction = transaction
            self._expires_at = self._monotonic() + self._cache_ttl_seconds
            return transaction

    async def generate(self, method: str, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("X transaction ID requires a full HTTP request URL")
        transaction = await self._current_transaction()
        value = await asyncio.to_thread(
            transaction.generate_transaction_id,
            method.upper(),
            parsed.path or "/",
        )
        if isinstance(value, bytes):
            value = value.decode("ascii")
        if not isinstance(value, str) or not value:
            raise RemoteDiscoveryError("gallery-dl generated an invalid X transaction ID")
        return value
