import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

DANBOORU_BASE = "https://danbooru.donmai.us"
UA = "auto-gallery/0.1 (reference provider)"
REQUEST_TIMEOUT = 15


def _get(path: str) -> dict | list | None:
    """GET request to Danbooru API."""
    url = f"{DANBOORU_BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except Exception as e:
        logger.warning("Danbooru API request failed: %s %s", url, e)
        return None


def search_by_url(source_url: str) -> list[dict]:
    """Search Danbooru artists by an exact source URL."""
    encoded = urllib.parse.quote(source_url, safe="")
    path = f"/artists.json?search[url_matches]={encoded}&limit=10"
    result = _get(path)
    if isinstance(result, list):
        return result
    return []


def search_by_pixiv_id(user_id: str) -> list[dict]:
    """Search Danbooru artists by constructing the exact Pixiv user URL."""
    # Build exact URL and use url_matches (same as search_by_url)
    source_url = f"https://www.pixiv.net/en/users/{user_id}"
    return search_by_url(source_url)


def search_by_name(name: str) -> list[dict]:
    """Search Danbooru artists by name (wildcard match)."""
    encoded = urllib.parse.quote(f"*{name}*", safe="*")
    path = f"/artists.json?search[any_name_matches]={encoded}&limit=10"
    result = _get(path)
    if isinstance(result, list):
        return result
    return []


def get_artist(artist_id: int) -> dict | None:
    """Get full Danbooru artist detail including URLs."""
    # Danbooru API doesn't include URLs in detail by default — fetch them separately
    artist = _get(f"/artists/{artist_id}.json")
    if not isinstance(artist, dict):
        return None
    urls = _get(f"/artists/{artist_id}.json?only=urls")
    if isinstance(urls, dict) and "urls" in urls:
        artist["urls"] = urls["urls"]
    else:
        artist["urls"] = []
    return artist


# URL patterns that are valid gallery-dl download sources
DOWNLOADABLE_PATTERNS = {
    "pixiv": [r"pixiv\.net/(?:en/)?users/\d+"],
    "iwara": [r"iwara\.tv/(?:users|profile)/\w+"],
}


def _classify_url(url: str) -> str:
    """Classify a URL into a link_type."""
    u = url.lower()
    # Pixiv sub-types
    if "sketch.pixiv.net" in u:
        return "pixiv_sketch"
    if "pixiv.net/stacc" in u:
        return "pixiv_stacc"
    if "pixiv.net/fanbox" in u or "fanbox.cc" in u:
        return "fanbox"
    if "pixiv.net" in u:
        return "pixiv"
    # Social / X
    if "twitter.com" in u or "x.com" in u:
        return "x"
    if "bsky.app" in u:
        return "bluesky"
    if "misskey" in u:
        return "misskey"
    if "mastodon" in u:
        return "mastodon"
    # Video platforms
    if "iwara.tv" in u:
        return "iwara"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "bilibili.com" in u:
        return "bilibili"
    if "nicovideo.jp" in u:
        return "nicovideo"
    if "vimeo.com" in u:
        return "vimeo"
    # Art platforms
    if "danbooru" in u:
        return "danbooru"
    if "deviantart.com" in u:
        return "deviantart"
    if "artstation.com" in u:
        return "artstation"
    if "pixiv.net" in u:
        return "pixiv"
    # Chinese platforms
    if "weibo.com" in u or "weibo.cn" in u:
        return "weibo"
    if "xiaohongshu.com" in u:
        return "xiaohongshu"
    if "bcy.net" in u:
        return "bcy"
    if "lofter.com" in u:
        return "lofter"
    # Funding / Shop
    if "fanbox" in u:
        return "fanbox"
    if "skeb.jp" in u:
        return "skeb"
    if "patreon.com" in u:
        return "patreon"
    if "boosty.to" in u:
        return "boosty"
    if "gumroad.com" in u:
        return "gumroad"
    if "fantia.jp" in u:
        return "fantia"
    # Social media
    if "instagram.com" in u:
        return "instagram"
    if "tumblr.com" in u:
        return "tumblr"
    if "facebook.com" in u:
        return "facebook"
    if "tiktok.com" in u:
        return "tiktok"
    if "threads.net" in u:
        return "threads"
    if "reddit.com" in u:
        return "reddit"
    # Other
    if "linktr.ee" in u:
        return "linktree"
    if "carrd.co" in u:
        return "carrd"
    if "about.me" in u:
        return "aboutme"
    return "website"


def is_downloadable_url(url: str) -> bool:
    """Check if a URL is a valid source for gallery-dl download."""
    import re
    link_type = _classify_url(url)
    if link_type not in DOWNLOADABLE_PATTERNS:
        return False
    for pattern in DOWNLOADABLE_PATTERNS[link_type]:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def extract_creator_links(artist: dict) -> list[dict]:
    """Extract creator_link candidates from a Danbooru artist record."""
    links = []

    # 1. Danbooru artist reference link (always)
    artist_id = artist["id"]
    links.append({
        "url": f"https://danbooru.donmai.us/artists/{artist_id}",
        "link_type": "danbooru",
        "source": "danbooru_reference",
        "confidence": 0.9,
        "is_verified": False,
        "notes": f"Danbooru artist tag: {artist['name']}"
                + (f" (aka: {', '.join(artist.get('other_names', []))})" if artist.get("other_names") else ""),
    })

    # 2. Source URLs from Danbooru artist record
    for u in artist.get("urls", []):
        raw_url = u.get("normalized_url") or u.get("url", "")
        if not raw_url:
            continue
        link_type = _classify_url(raw_url)
        is_active = u.get("is_active", True)
        links.append({
            "url": raw_url,
            "link_type": link_type,
            "source": "danbooru_reference",
            "confidence": 0.7 if is_active else 0.4,
            "is_verified": False,
            "notes": f"From Danbooru artist #{artist_id} ({artist['name']})"
                     + (" [inactive]" if not is_active else ""),
        })

    return links


def search_and_extract(source_url: str | None = None,
                       pixiv_id: str | None = None,
                       artist_name: str | None = None) -> tuple[dict | None, list[dict]]:
    """Search Danbooru for an artist and extract creator_link candidates.

    Returns (artist_detail, list_of_link_dicts). artist_detail is None if no match.
    """
    candidates = []

    # Try exact URL match first (most specific)
    if source_url:
        candidates = search_by_url(source_url)

    # Try Pixiv ID match
    if not candidates and pixiv_id:
        candidates = search_by_pixiv_id(pixiv_id)

    # Try name match as last resort
    if not candidates and artist_name:
        candidates = search_by_name(artist_name)

    if not candidates:
        return None, []

    # Get full detail of the best match (first result)
    best = candidates[0]
    artist_id = best["id"]
    artist = get_artist(artist_id)
    if not artist:
        # Fall back to listing data if detail fails
        artist = best

    links = extract_creator_links(artist)
    return artist, links
