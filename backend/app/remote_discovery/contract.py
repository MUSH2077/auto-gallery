"""Provider-neutral contracts for account-private remote follow discovery."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from app.services.remote_credentials import (
    DownloadAuthenticationOverride,
    RedactedCredentials,
)


RemoteSource = Literal["pixiv", "x", "bilibili"]
RemoteWorkType = Literal["illust", "manga", "ugoira"]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, set):
        return frozenset(_freeze(child) for child in value)
    return value


def _require_remote_source(source: str) -> None:
    if source not in {"pixiv", "x", "bilibili"}:
        raise ValueError("remote source is not supported")


def _require_https_url(value: str | None, field_name: str) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must use HTTPS")


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
        _require_remote_source(self.source)
        if not self.source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        _require_https_url(self.profile_url, "remote profile URL")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class RemoteCreatorProfile:
    source: RemoteSource
    source_creator_id: str
    display_name: str | None
    username: str | None
    profile_url: str
    avatar_url: str | None
    comment: str | None
    work_counts: Mapping[str, int]
    is_followed: bool | None
    fetched_at: datetime

    def __post_init__(self) -> None:
        _require_remote_source(self.source)
        if not self.source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        _require_https_url(self.profile_url, "remote profile URL")
        _require_https_url(self.avatar_url, "remote avatar URL")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        if not isinstance(self.work_counts, Mapping):
            raise TypeError("work_counts must be a mapping")
        for name, value in self.work_counts.items():
            if not str(name).strip() or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("work_counts must contain non-negative integer values")
        if self.is_followed is not None and not isinstance(self.is_followed, bool):
            raise ValueError("is_followed must be a boolean or None")
        object.__setattr__(self, "work_counts", _freeze(self.work_counts))


@dataclass(frozen=True)
class RemoteWorkPreview:
    source: RemoteSource
    source_work_id: str
    source_creator_id: str
    title: str
    work_url: str
    created_at: datetime
    work_type: RemoteWorkType
    page_count: int
    x_restrict: int
    thumbnail_url: str | None
    preview_urls: tuple[str, ...] | list[str]

    def __post_init__(self) -> None:
        _require_remote_source(self.source)
        if not self.source_work_id.strip():
            raise ValueError("source_work_id must not be empty")
        if not self.source_creator_id.strip():
            raise ValueError("source_creator_id must not be empty")
        if self.work_type not in {"illust", "manga", "ugoira"}:
            raise ValueError("remote work type is not supported")
        if isinstance(self.page_count, bool) or not isinstance(self.page_count, int) or self.page_count < 1:
            raise ValueError("page_count must be a positive integer")
        if self.x_restrict not in {0, 1, 2}:
            raise ValueError("x_restrict must be 0, 1, or 2")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        _require_https_url(self.work_url, "remote work URL")
        _require_https_url(self.thumbnail_url, "remote thumbnail URL")
        preview_urls = tuple(self.preview_urls)
        for url in preview_urls:
            _require_https_url(url, "remote preview URL")
        object.__setattr__(self, "preview_urls", preview_urls)


@dataclass(frozen=True)
class RemoteWorkPage:
    items: tuple[RemoteWorkPreview, ...] | list[RemoteWorkPreview]
    done: bool
    next_cursor: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if any(not isinstance(item, RemoteWorkPreview) for item in items):
            raise TypeError("remote work page items must be remote work previews")
        sources = {item.source for item in items}
        if len(sources) > 1:
            raise ValueError("remote work page items must all use the same source")
        if self.done and self.next_cursor is not None:
            raise ValueError("a done remote work page cannot expose a next cursor")
        if not self.done and self.next_cursor is None:
            raise ValueError("an incomplete remote work page requires a next cursor")
        object.__setattr__(self, "items", items)
        if self.next_cursor is not None:
            if not isinstance(self.next_cursor, Mapping) or not self.next_cursor:
                raise ValueError("next cursor must be a non-empty mapping or None")
            object.__setattr__(self, "next_cursor", _freeze(self.next_cursor))

    @property
    def is_complete(self) -> bool:
        return self.next_cursor is None


@dataclass(frozen=True)
class RemoteCreatorDetail:
    profile: RemoteCreatorProfile
    works: RemoteWorkPage

    def __post_init__(self) -> None:
        for work in self.works.items:
            if work.source != self.profile.source or work.source_creator_id != self.profile.source_creator_id:
                raise ValueError("remote creator detail works must belong to the same creator")


@dataclass(frozen=True)
class RemoteWorkState:
    source: RemoteSource
    source_work_id: str
    fetched_at: datetime
    total_views: int
    total_bookmarks: int
    is_bookmarked: bool

    def __post_init__(self) -> None:
        _require_remote_source(self.source)
        if not self.source_work_id.strip():
            raise ValueError("source_work_id must not be empty")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        for name in ("total_views", "total_bookmarks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.is_bookmarked, bool):
            raise ValueError("is_bookmarked must be a boolean")


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

    async def fetch_work_state(
        self,
        credentials: RedactedCredentials | Mapping[str, Any],
        *,
        source_work_id: str,
    ) -> RemoteWorkState:
        raise NotImplementedError(f"{self.source} does not support remote work state")

    async def fetch_creator_profile(
        self,
        credentials: RedactedCredentials | Mapping[str, Any],
        *,
        source_creator_id: str,
    ) -> RemoteCreatorProfile:
        raise NotImplementedError(f"{self.source} does not support remote creator profiles")

    async def fetch_creator_detail(
        self,
        credentials: RedactedCredentials | Mapping[str, Any],
        *,
        source_creator_id: str,
        page_size: int = 20,
    ) -> RemoteCreatorDetail:
        raise NotImplementedError(f"{self.source} does not support remote creator details")

    async def fetch_creator_works(
        self,
        credentials: RedactedCredentials | Mapping[str, Any],
        *,
        source_creator_id: str,
        cursor: Mapping[str, Any] | None = None,
        page_size: int = 20,
    ) -> RemoteWorkPage:
        raise NotImplementedError(f"{self.source} does not support remote creator works")

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
