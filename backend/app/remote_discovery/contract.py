"""Provider-neutral contracts for account-private remote follow discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from app.services.remote_credentials import (
    DownloadAuthenticationOverride,
    RedactedCredentials,
)


RemoteSource = Literal["pixiv", "x", "bilibili"]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, set):
        return frozenset(_freeze(child) for child in value)
    return value


@dataclass(frozen=True)
class RemoteCollection:
    id: str
    name: str
    selector: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("remote collection id must not be empty")
        if not self.name.strip():
            raise ValueError("remote collection name must not be empty")
        if not isinstance(self.selector, Mapping):
            raise TypeError("remote collection selector must be a mapping")
        object.__setattr__(self, "selector", _freeze(self.selector))


@dataclass(frozen=True)
class RemoteCandidateIdentity:
    source: RemoteSource
    source_creator_id: str
    profile_url: str | None
    display_name: str | None = None
    username: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in {"pixiv", "x", "bilibili"}:
            raise ValueError("remote candidate source is not supported")
        if not self.source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        if self.profile_url:
            parsed = urlsplit(self.profile_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("remote profile URL must use HTTPS")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class DiscoveryPage:
    items: tuple[RemoteCandidateIdentity, ...] | list[RemoteCandidateIdentity]
    done: bool
    next_cursor: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if any(not isinstance(item, RemoteCandidateIdentity) for item in items):
            raise TypeError("discovery page items must be remote candidate identities")
        sources = {item.source for item in items}
        if len(sources) > 1:
            raise ValueError("discovery page items must all use the same source")
        if self.done and self.next_cursor is not None:
            raise ValueError("a done discovery page cannot expose a next cursor")
        if not self.done and self.next_cursor is None:
            raise ValueError("an incomplete discovery page requires a next cursor")
        object.__setattr__(self, "items", items)
        if self.next_cursor is not None:
            if not isinstance(self.next_cursor, Mapping) or not self.next_cursor:
                raise ValueError("next cursor must be a non-empty mapping or None")
            object.__setattr__(self, "next_cursor", _freeze(self.next_cursor))

    @property
    def is_complete(self) -> bool:
        return self.next_cursor is None


class RemoteDiscoveryAdapter(ABC):
    """The only provider-specific boundary used by discovery services."""

    source: RemoteSource
    auth_methods: tuple[str, ...]

    @abstractmethod
    async def list_collections(
        self, credentials: RedactedCredentials | Mapping[str, Any]
    ) -> tuple[RemoteCollection, ...]: ...

    @abstractmethod
    async def fetch_page(
        self,
        credentials: RedactedCredentials | Mapping[str, Any],
        *,
        selector: Mapping[str, Any] | None = None,
        cursor: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> DiscoveryPage: ...

    async def discover(
        self,
        credentials: RedactedCredentials | Mapping[str, Any],
        *,
        collection_id: str | None = None,
        cursor: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> DiscoveryPage:
        """Compatibility alias for callers written before the final contract."""

        selector = {"collection_id": collection_id} if collection_id else None
        return await self.fetch_page(
            credentials,
            selector=selector,
            cursor=cursor,
            page_size=page_size,
        )

    @abstractmethod
    def build_download_auth(
        self, credentials: RedactedCredentials | Mapping[str, Any]
    ) -> DownloadAuthenticationOverride | None: ...
    @abstractmethod
    async def validate_account(
        self, credentials: RedactedCredentials | Mapping[str, Any]
    ) -> RemoteCandidateIdentity: ...
