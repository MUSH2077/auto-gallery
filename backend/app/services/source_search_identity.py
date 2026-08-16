"""Shared source-identity parsing for the canonical search language."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from app.providers import registry


IDENTITY_SOURCES = frozenset({
    "pixiv",
    "x",
    "iwara",
    "danbooru",
    "weibo",
    "bilibili",
    "pinterest",
    "lofter",
    "manual",
})

PUBLIC_URL_SOURCES = (
    "pixiv",
    "x",
    "iwara",
    "danbooru",
    "weibo",
    "bilibili",
    "pinterest",
    "lofter",
)


def _source_for_hostname(hostname: str) -> str | None:
    host = hostname.rstrip(".").lower()
    if host == "pixiv.net" or host.endswith(".pixiv.net"):
        return "pixiv"
    if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
        return "x"
    if host == "iwara.tv" or host.endswith(".iwara.tv"):
        return "iwara"
    if host == "danbooru.donmai.us":
        return "danbooru"
    if host in {"weibo.com", "weibo.cn"} or host.endswith((".weibo.com", ".weibo.cn")):
        return "weibo"
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return "bilibili"
    if host.startswith("pinterest.") or host.startswith("www.pinterest."):
        return "pinterest"
    if host.endswith(".lofter.com"):
        return "lofter"
    return None


@dataclass(frozen=True)
class ParsedSourceURL:
    source: str
    kind: Literal["creator", "work"]
    normalized_url: str


def normalize_source_name(value: str) -> str:
    normalized = value.strip().lower()
    return "x" if normalized == "twitter" else normalized


def parse_source_identity(value: str) -> tuple[str, str]:
    """Parse the public ``source/opaque-id`` identity value."""

    if "/" not in value:
        raise ValueError("Source identities use the format source/id.")
    raw_source, raw_identity = value.split("/", 1)
    source = normalize_source_name(raw_source)
    identity = raw_identity.strip()
    if source not in IDENTITY_SOURCES:
        raise ValueError(f"Unsupported identity source: {raw_source.strip() or raw_source}")
    if not identity:
        raise ValueError("Source identity is missing its ID.")
    if any(character.isspace() for character in identity):
        raise ValueError("Source identity IDs cannot contain whitespace.")
    return source, identity


def parse_source_url(value: str) -> ParsedSourceURL | None:
    """Classify a supported public URL without performing network I/O."""

    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    source = _source_for_hostname(urlparse(candidate).hostname or "")
    if source not in PUBLIC_URL_SOURCES:
        return None
    parsed = registry.get(source).parse_search_url(candidate)
    if parsed is None:
        return None
    return ParsedSourceURL(
        source=source,
        kind=parsed.kind,
        normalized_url=parsed.normalized_url,
    )
