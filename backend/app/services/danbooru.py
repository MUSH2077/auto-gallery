import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)

DANBOORU_BASE = "https://danbooru.donmai.us"
UA = "auto-gallery/0.1 (reference provider)"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, doubles each retry


class DanbooruUnavailableError(Exception):
    """Danbooru API unreachable (network/proxy failure) — NOT a lookup miss.

    Callers must never record "not found" for a request that never reached
    Danbooru; an empty search result is a 200 with [], and a missing artist
    detail is a 404 (both return None/[] from this module without raising).
    """


def _get_opener():
    """Get a urllib opener with proxy config if enabled.

    Uses a sync DB query via the cached proxy config to avoid async event-loop
    conflicts when called from asyncio.to_thread().
    """
    try:
        from app.services.proxy import get_cached_proxy_config, apply_proxy_to_urllib

        config = get_cached_proxy_config()
        opener = apply_proxy_to_urllib(config)
        if opener:
            logger.info("Danbooru API using proxy: %s", config.get("http_proxy", "not set"))
            return opener
        else:
            logger.info("Danbooru API direct (proxy disabled)")
    except Exception:
        logger.warning("Failed to load proxy config for Danbooru, using direct", exc_info=True)
    return urllib.request.build_opener()


_opener = None


def _get(path: str) -> dict | list | None:
    """GET request to Danbooru API with retry on transient errors.

    Returns None only for HTTP 404 (resource genuinely absent). Raises
    DanbooruUnavailableError once retries are exhausted so callers can
    distinguish "Danbooru is down" from "no such artist".
    """
    global _opener
    url = f"{DANBOORU_BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            if _opener is None:
                _opener = _get_opener()
            with _opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last_error = e
        except Exception as e:
            last_error = e
        if attempt < MAX_RETRIES - 1:
            wait = RETRY_BACKOFF * (2 ** attempt)
            logger.debug("Danbooru API attempt %d/%d failed, retrying in %ds: %s",
                         attempt + 1, MAX_RETRIES, wait, last_error)
            time.sleep(wait)
            # Reset opener on SSL errors so it reconnects fresh
            if "SSL" in str(last_error) or "ssl" in str(last_error).lower():
                _opener = None

    logger.warning("Danbooru API unavailable after %d attempts: %s %s",
                   MAX_RETRIES, url, last_error)
    raise DanbooruUnavailableError(f"{url}: {last_error}")


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
    source_url = f"https://www.pixiv.net/users/{user_id}"
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
    try:
        urls = _get(f"/artists/{artist_id}.json?only=urls")
    except DanbooruUnavailableError:
        urls = None  # keep the detail we already fetched; URLs are optional
    if isinstance(urls, dict) and "urls" in urls:
        artist["urls"] = urls["urls"]
    else:
        artist["urls"] = []
    return artist


# URL patterns that are valid gallery-dl download sources
DOWNLOADABLE_PATTERNS = {
    "pixiv": [r"pixiv\.net/(?:en/)?users/\d+"],
    "x": [r"(?:twitter\.com|x\.com)/(?!i/)(?!intent)\w+"],
    "weibo": [r"weibo\.(?:com|cn)/(?:u/\d+|[\w\u4e00-\u9fff]+)"],
    "iwara": [r"iwara\.tv/(?:users|profile)/[\w-]+"],
    "danbooru": [r"danbooru\.donmai\.us/(?:posts\?tags=|artists/\d+|pools/\d+)"],
    "pinterest": [r"pinterest\.\w+(?:\.\w+)?/(?:pin/\d+|(?:[\w.-]+/pins|[\w.-]+/[\w.-]+/?))"],
    "lofter": [r"^https?://(?!www\.)[\w-]+\.lofter\.com"],
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
    # Social / X  (use regex to avoid matching substrings like ax.com)
    if "twitter.com" in u or re.search(r"(?:^|[./@])x\.com(?:[/?#:]|$)", u):
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
    # Chinese platforms
    if "weibo.com" in u or "weibo.cn" in u:
        return "weibo"
    if "xiaohongshu.com" in u:
        return "xiaohongshu"
    if "bcy.net" in u:
        return "bcy"
    if "pinterest.com" in u or "pin.it" in u:
        return "pinterest"
    # Lofter: only subdomain blogs (user.lofter.com), not www.lofter.com
    if ".lofter.com" in u and "www.lofter.com" not in u:
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
    try:
        artist = get_artist(artist_id)
    except DanbooruUnavailableError:
        # The search itself succeeded — a transient failure fetching detail
        # must not discard the match. Listing data is enough for linking.
        artist = None
    if not artist:
        # Fall back to listing data if detail fails
        artist = best

    links = extract_creator_links(artist)
    return artist, links


# ── Auth + Favorites ──

_auth_cache: dict | None = None


def _get_auth() -> dict:
    """Read Danbooru auth credentials from gallery-dl config.json."""
    global _auth_cache
    if _auth_cache is not None:
        return _auth_cache

    try:
        import json, os
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
            extractor = cfg.get("extractor", {}).get("danbooru", {})
            _auth_cache = {
                "username": extractor.get("username", ""),
                "password": extractor.get("password", ""),
                "api_key": extractor.get("api-key", ""),
            }
            return _auth_cache
    except Exception:
        logger.debug("Failed to read Danbooru auth from config", exc_info=True)
    _auth_cache = {}
    return _auth_cache


def _add_auth(req: urllib.request.Request) -> urllib.request.Request:
    """Add Danbooru auth to a request if credentials are configured."""
    auth = _get_auth()
    api_key = auth.get("api_key", "")
    username = auth.get("username", "")
    if api_key:
        if username:
            # HTTP Basic Auth (Danbooru: username + api_key as password)
            import base64
            creds = base64.b64encode(f"{username}:{api_key}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
        else:
            # API key as query param
            req.add_header("X-Api-Key", api_key)
    return req

