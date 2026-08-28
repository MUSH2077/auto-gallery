"""Lazy X web-client transaction IDs backed by pinned gallery-dl."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import logging
import time
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.remote_discovery.common import (
    MalformedRemoteResponse,
    RemoteDiscoveryError,
    RemoteRateLimited,
    RemoteReauthenticationRequired,
)


class XTransactionIdProvider(Protocol):
    async def generate(self, method: str, url: str, *, cookie: str) -> str: ...


@dataclass(frozen=True)
class XTransactionBootstrapResponse:
    status_code: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "XTransactionBootstrapResponse("
            f"status_code={self.status_code}, text=<redacted>, headers=<redacted>)"
        )


class XTransactionBootstrapTransport(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> XTransactionBootstrapResponse: ...


class HttpxXTransactionBootstrapTransport:
    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> XTransactionBootstrapResponse:
        import httpx

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
        return XTransactionBootstrapResponse(
            response.status_code,
            response.text,
            dict(response.headers),
        )


def _checked_bootstrap_text(response: XTransactionBootstrapResponse) -> str:
    if response.status_code in {401, 403}:
        raise RemoteReauthenticationRequired(response.status_code)
    if response.status_code == 429:
        raw_retry = response.headers.get("Retry-After") or response.headers.get(
            "retry-after"
        )
        try:
            retry_after = int(raw_retry) if raw_retry is not None else None
        except (TypeError, ValueError):
            retry_after = None
        raise RemoteRateLimited(retry_after)
    if not 200 <= response.status_code < 300:
        raise RemoteDiscoveryError(
            f"X transaction bootstrap failed with HTTP {response.status_code}"
        )
    if not isinstance(response.text, str):
        raise MalformedRemoteResponse("X transaction bootstrap returned invalid text")
    return response.text


def _ondemand_url(homepage: str) -> str:
    from gallery_dl import text

    ondemand_pos = homepage.find('"ondemand.s"')
    if ondemand_pos < 0:
        raise RemoteDiscoveryError(
            "X authenticated homepage lacks legacy responsive-web transaction state; "
            "retry later or reauthenticate the Cookie account"
        )
    ondemand_key = text.rextr(homepage, ",", ":", ondemand_pos)
    ondemand_s = text.extract(
        homepage,
        ondemand_key + ':"',
        '"',
        ondemand_pos,
    )[0]
    if not ondemand_s:
        raise RemoteDiscoveryError(
            "X authenticated homepage has malformed legacy responsive-web transaction state; "
            "retry later or reauthenticate the Cookie account"
        )
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
    MAX_CACHED_ACCOUNTS = 32

    def __init__(
        self,
        *,
        bootstrap_transport: XTransactionBootstrapTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        if cache_ttl_seconds < 1:
            raise ValueError("X transaction state cache TTL must be positive")
        self._bootstrap_transport = (
            bootstrap_transport or HttpxXTransactionBootstrapTransport()
        )
        self._monotonic = monotonic
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_transactions: OrderedDict[bytes, tuple[Any, float]] = OrderedDict()
        self._initialize_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "GalleryDlXTransactionIdProvider(credentials=<ephemeral>, cache=<memory>)"

    def _cached(self, cache_key: bytes, now: float):
        cached = self._cached_transactions.get(cache_key)
        if cached is None:
            return None
        transaction, expires_at = cached
        if now >= expires_at:
            del self._cached_transactions[cache_key]
            return None
        self._cached_transactions.move_to_end(cache_key)
        return transaction

    async def _current_transaction(self, cookie: str):
        cache_key = hashlib.sha256(cookie.encode("utf-8")).digest()
        now = self._monotonic()
        if transaction := self._cached(cache_key, now):
            return transaction
        async with self._initialize_lock:
            now = self._monotonic()
            if transaction := self._cached(cache_key, now):
                return transaction
            headers = {"Cookie": cookie}
            homepage_response = await self._bootstrap_transport.fetch(
                "https://x.com/",
                headers=headers,
            )
            homepage = _checked_bootstrap_text(homepage_response)
            ondemand_url = await asyncio.to_thread(_ondemand_url, homepage)
            ondemand_response = await self._bootstrap_transport.fetch(
                ondemand_url,
                headers=headers,
            )
            ondemand_javascript = _checked_bootstrap_text(ondemand_response)
            transaction = await asyncio.to_thread(
                _initialize_gallery_dl_transaction,
                homepage,
                ondemand_url,
                ondemand_javascript,
            )
            self._cached_transactions[cache_key] = (
                transaction,
                self._monotonic() + self._cache_ttl_seconds,
            )
            self._cached_transactions.move_to_end(cache_key)
            while len(self._cached_transactions) > self.MAX_CACHED_ACCOUNTS:
                self._cached_transactions.popitem(last=False)
            return transaction

    async def generate(self, method: str, url: str, *, cookie: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("X transaction ID requires a full HTTP request URL")
        if not isinstance(cookie, str) or not cookie:
            raise RemoteReauthenticationRequired(
                401,
                "X transaction bootstrap requires authenticated Cookie credentials",
            )
        transaction = await self._current_transaction(cookie)
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
