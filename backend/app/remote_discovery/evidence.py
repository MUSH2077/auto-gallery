"""Deterministic, versioned evidence extraction for remote-follow candidates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
import re
from urllib.parse import urlsplit


EVIDENCE_VERSION = 1
RECENT_VISUAL_DAYS = 90

_ART_BIO_PATTERNS = (
    re.compile(
        r"\b(?:illustrator|artist|concept\s+artist|comic\s+artist|manga\s+artist|"
        r"animator|fan\s*artist|commissions?\s+(?:open|available))\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:イラストレーター|絵師|原画家|漫画家|お絵描き|絵を描いて|イラスト(?:・原画)?を描いて|原画を描いて|お仕事募集)"),
    re.compile(r"(?:插画师|插畫師|画师|畫師|绘师|繪師|原画师|原畫師|漫画家|漫畫家|约稿|約稿|接稿)"),
)

_SOURCE_HOSTS = {
    "pixiv": ("pixiv.net", "www.pixiv.net"),
    "x": ("x.com", "www.x.com", "twitter.com", "www.twitter.com"),
    "bilibili": ("bilibili.com", "www.bilibili.com", "space.bilibili.com"),
    "danbooru": ("danbooru.donmai.us",),
    "iwara": ("iwara.tv", "www.iwara.tv"),
    "pinterest": ("pinterest.com", "www.pinterest.com"),
    "lofter": ("lofter.com", "www.lofter.com"),
    "weibo": ("weibo.com", "www.weibo.com"),
}
_SHORTENER_HOSTS = {
    "t.co",
    "bit.ly",
    "b23.tv",
    "tinyurl.com",
    "goo.gl",
}


def expanded_profile_links(values: Iterable[object]) -> tuple[str, ...]:
    """Keep explicit HTTPS targets for verified-link matching, never opaque short URLs."""

    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https" or not host or host in _SHORTENER_HOSTS:
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def art_focused_bio(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and any(pattern.search(text) for pattern in _ART_BIO_PATTERNS))


def _source_for_host(host: str) -> str | None:
    normalized = host.casefold().rstrip(".")
    for source, hosts in _SOURCE_HOSTS.items():
        if normalized in hosts or any(normalized.endswith(f".{item}") for item in hosts):
            return source
    return None


def supported_profile_links(
    values: Iterable[object],
    *,
    source: str,
) -> tuple[str, ...]:
    """Return expanded, provider-recognized cross-site URLs in stable order."""

    result: list[str] = []
    for value in expanded_profile_links(values):
        parsed = urlsplit(value)
        matched_source = _source_for_host(parsed.hostname)
        if matched_source is None or matched_source == source:
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def _as_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def has_recent_visual_post(
    posts: Iterable[Mapping[str, object]],
    *,
    now: datetime | None = None,
) -> bool:
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    threshold = checked_at - timedelta(days=RECENT_VISUAL_DAYS)
    for post in posts:
        created_at = _as_utc(post.get("created_at"))
        if created_at is not None and created_at >= threshold and bool(post.get("has_visual_media")):
            return True
    return False
