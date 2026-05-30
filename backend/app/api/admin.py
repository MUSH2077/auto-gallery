import asyncio
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequireAdmin
from app.database import get_db
from app.models.system_setting import SystemSetting

router = APIRouter(dependencies=[RequireAdmin])

# ── Settings schemas ──

class DedupSettings(BaseModel):
    source_level_enabled: bool = False
    cross_source_enabled: bool = False
    auto_merge: bool = False
    phash_threshold: int = 8

class SubscriptionDefaults(BaseModel):
    default_sync_interval_hours: int = 6
    scheduler_scan_interval_minutes: int = 60
    schedule_mode: str = "interval"
    scheduled_times: str = ""
    auto_enable_sources: str = "pixiv"

DEFAULT_SUB = {"default_sync_interval_hours": 6, "scheduler_scan_interval_minutes": 60, "schedule_mode": "interval", "scheduled_times": "", "auto_enable_sources": "pixiv"}

class DownloadDefaults(BaseModel):
    timeout_seconds: int = 600
    max_retries: int = 3
    retry_backoff_base_seconds: int = 60
    max_posts: int = 200
    skip_ai_generated: bool = False

class ProxySettings(BaseModel):
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1,::1"
    enabled: bool = False

DEFAULT_PROXY = {"http_proxy": "", "https_proxy": "", "no_proxy": "localhost,127.0.0.1,::1", "enabled": False}

class AdminSettingsUpdate(BaseModel):
    dedup: DedupSettings | None = None
    subscription_defaults: SubscriptionDefaults | None = None
    download_defaults: DownloadDefaults | None = None
    proxy: ProxySettings | None = None


# ── Helpers ──

async def _get_setting(db: AsyncSession, key: str, default: dict = None) -> dict:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else (default or {})


async def _put_setting(db: AsyncSession, key: str, value: dict):
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    await db.commit()

    # Invalidate caches when relevant settings change
    if key == "proxy":
        try:
            from app.services.proxy import clear_proxy_cache
            clear_proxy_cache()
        except Exception:
            pass


DEFAULT_DEDUP = {"source_level_enabled": False, "cross_source_enabled": False, "auto_merge": False, "phash_threshold": 8}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False}


# ── Settings ──

@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    dedup = await _get_setting(db, "dedup", DEFAULT_DEDUP)
    sub = await _get_setting(db, "subscription_defaults", DEFAULT_SUB)
    dl = await _get_setting(db, "download_defaults", DEFAULT_DL)
    proxy = await _get_setting(db, "proxy", DEFAULT_PROXY)
    return {"dedup": dedup, "subscription_defaults": sub, "download_defaults": dl, "proxy": proxy}


@router.put("/settings")
async def update_settings(data: AdminSettingsUpdate, db: AsyncSession = Depends(get_db)):
    if data.dedup is not None:
        await _put_setting(db, "dedup", data.dedup.model_dump())
    if data.subscription_defaults is not None:
        await _put_setting(db, "subscription_defaults", data.subscription_defaults.model_dump())
    if data.download_defaults is not None:
        await _put_setting(db, "download_defaults", data.download_defaults.model_dump())
    if data.proxy is not None:
        await _put_setting(db, "proxy", data.proxy.model_dump())
    return {"status": "ok", "message": "Settings saved to database"}


# ── Proxy Test ──

@router.post("/proxy/test")
async def test_proxy_connectivity(db: AsyncSession = Depends(get_db)):
    """Test connectivity through the configured proxy to key external sites."""
    import urllib.request
    import urllib.error
    import ssl
    import time

    config = await _get_setting(db, "proxy", DEFAULT_PROXY)
    enabled = config.get("enabled", False)
    ssl_verify = config.get("ssl_verify", True)

    # Build SSL context — skip verification if user disabled it (MITM proxy)
    ssl_ctx = ssl.create_default_context()
    if not ssl_verify:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    # Build proxy opener
    opener = None
    if enabled:
        proxies = {}
        http_proxy = config.get("http_proxy", "")
        https_proxy = config.get("https_proxy", "")
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        if proxies:
            proxy_handler = urllib.request.ProxyHandler(proxies)
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ssl_ctx))

    if opener is None:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))

    # ── Proxy reachability check ──
    proxy_reachable = None
    proxy_reachable_error = ""
    if enabled:
        proxy_url = config.get("http_proxy") or config.get("https_proxy", "")
        if proxy_url:
            import socket, re
            m = re.match(r'https?://([^:/]+):?(\d+)?', proxy_url)
            if m:
                host = m.group(1)
                port = int(m.group(2)) if m.group(2) else 7890
                try:
                    sock = socket.create_connection((host, port), timeout=5)
                    sock.close()
                    proxy_reachable = True
                except Exception as e:
                    proxy_reachable = False
                    proxy_reachable_error = f"Cannot connect to {host}:{port} — {e}"

    direct_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))

    targets = [
        {"name": "Pixiv", "url": "https://www.pixiv.net"},
        {"name": "Pixiv API", "url": "https://app-api.pixiv.net"},
        {"name": "Danbooru", "url": "https://danbooru.donmai.us"},
        {"name": "Danbooru API", "url": "https://danbooru.donmai.us/artists.json?limit=1"},
        {"name": "Iwara", "url": "https://www.iwara.tv"},
        {"name": "Iwara API", "url": "https://api.iwara.tv"},
        {"name": "Twitter/X", "url": "https://x.com"},
        {"name": "GitHub", "url": "https://github.com"},
        {"name": "Google", "url": "https://www.google.com"},
    ]

    import concurrent.futures
    TEST_TIMEOUT = 3  # per-test timeout, all 9 run concurrently with 9 workers

    def _test_one(t):
        name, url = t["name"], t["url"]
        # Direct
        d_ok, d_ms, d_err = False, 0, ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "auto-gallery/0.1"})
            start = time.monotonic()
            with direct_opener.open(req, timeout=TEST_TIMEOUT) as resp:
                d_ok = resp.status < 500
            d_ms = int((time.monotonic() - start) * 1000)
        except urllib.error.HTTPError as e:
            d_err, d_ok = f"HTTP {e.code}", e.code < 500
        except urllib.error.URLError as e:
            d_err = str(e.reason)[:120] if e.reason else str(e)[:120]
        except Exception as e:
            d_err = str(e)[:120]

        # Proxy
        p_ok, p_ms, p_err = False, 0, ""
        if enabled:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "auto-gallery/0.1"})
                start = time.monotonic()
                with opener.open(req, timeout=TEST_TIMEOUT) as resp:
                    p_ok = resp.status < 500
                p_ms = int((time.monotonic() - start) * 1000)
            except urllib.error.HTTPError as e:
                p_err, p_ok = f"HTTP {e.code}", e.code < 500
            except urllib.error.URLError as e:
                p_err = str(e.reason)[:120] if e.reason else str(e)[:120]
            except Exception as e:
                p_err = str(e)[:120]

        return {"name": name, "url": url,
                "direct_ok": d_ok, "direct_ms": d_ms, "direct_error": d_err,
                "proxy_ok": p_ok if enabled else None, "proxy_ms": p_ms if enabled else None,
                "proxy_error": p_err if enabled else ""}

    import concurrent.futures
    logger.info("Proxy test starting: enabled=%s proxy=%s targets=%d", enabled,
                config.get("http_proxy", "not set"), len(targets))
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
        results = list(executor.map(_test_one, targets))
    ok = sum(1 for r in results if r["proxy_ok"])
    fail = sum(1 for r in results if r["proxy_ok"] is False)
    logger.info("Proxy test complete: %d OK, %d FAIL, %d total (reachable=%s)",
                ok, fail, len(results), proxy_reachable)

    return {
        "proxy_enabled": enabled,
        "proxy_reachable": proxy_reachable,
        "proxy_reachable_error": proxy_reachable_error,
        "proxy_config": {
            "http": config.get("http_proxy", "not set"),
            "https": config.get("https_proxy", "not set"),
        },
        "results": results,
    }


# ── Data Management ──

ENTITIES = {
    "works": ["work_source_tags", "work_tags", "asset_sources", "assets", "work_sources", "works"],
    "creators": ["import_jobs", "download_jobs", "subscription_sources", "subscriptions", "source_creators", "creator_links", "creators"],
    "downloads": ["import_jobs", "download_jobs"],
    "jobs": ["import_jobs", "download_jobs"],
    "tags": ["work_source_tags", "work_tags", "tags"],
    "settings": [],  # Special: reset to defaults
}


@router.post("/clear/{entity}")
async def clear_entity(entity: str, db: AsyncSession = Depends(get_db)):
    """Clear a specific entity or 'all' for complete cleanup."""
    from sqlalchemy import text
    import shutil, os

    if entity == "all":
        order = [
            "work_source_tags", "work_tags", "asset_sources", "assets", "work_sources", "works",
            "import_jobs", "download_jobs",
            "subscription_sources", "subscriptions",
            "source_creators", "creator_links", "creators",
            "tags",
        ]
        results = {}
        for table in order:
            r = await db.execute(text(f"DELETE FROM {table}"))
            results[table] = r.rowcount
        await db.commit()
        _clear_files([str(settings.download_root), str(settings.library_root)])
        results["files"] = "downloads + library cleared"
        # Also clear Meilisearch indexes
        try:
            from app.services.search import SearchService
            svc = SearchService(db)
            await svc.delete_all_works()
            logger.info("Cleared Meilisearch indexes after 'all' clear")
        except Exception:
            logger.warning("Failed to clear Meilisearch", exc_info=True)
        return {"status": "ok", "message": "All data cleared", "deleted": results}

    tables = ENTITIES.get(entity)
    if tables is None:
        raise HTTPException(status_code=400, detail=f"Unknown entity: {entity}. Valid: all, works, creators, downloads")

    results = {}
    for table in tables:
        r = await db.execute(text(f"DELETE FROM {table}"))
        results[table] = r.rowcount
    await db.commit()

    # Clear files + Meilisearch for works-related entities
    if entity == "works":
        _clear_files([str(settings.download_root), str(settings.library_root)])
        results["files"] = "downloads + library cleared"
        try:
            from app.services.search import SearchService
            svc = SearchService(db)
            await svc.delete_all_works()
            logger.info("Cleared Meilisearch index after 'works' clear")
        except Exception:
            logger.warning("Failed to clear Meilisearch", exc_info=True)

    return {"status": "ok", "message": f"Cleared {entity}", "deleted": results}


def _clear_files(paths: list[str]):
    """Remove all files in given directories (preserving the root dirs)."""
    import logging
    import shutil, os
    _log = logging.getLogger(__name__)
    for p in paths:
        try:
            for item in os.listdir(p):
                item_path = os.path.join(p, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    os.unlink(item_path)
        except Exception:
            _log.warning("Failed to clear files in %s", p, exc_info=True)


@router.post("/reset-settings")
async def reset_settings(db: AsyncSession = Depends(get_db)):
    """Reset all system settings to defaults."""
    from app.models.system_setting import SystemSetting
    result = await db.execute(select(SystemSetting))
    for row in result.scalars().all():
        await db.delete(row)
    await db.commit()
    return {"status": "ok", "message": "All settings reset to defaults"}


# ── Scheduler ──

@router.post("/scheduler/sync-now")
async def trigger_sync_now():
    """Manually trigger a subscription sync scan."""
    import redis as redis_lib
    from rq import Queue
    from app.config import settings as app_settings
    r = redis_lib.from_url(app_settings.redis_url)
    q = Queue(name="scheduled", connection=r)
    job = q.enqueue("app.jobs.subscription_sync.sync_subscriptions")
    return {"status": "ok", "message": "Sync triggered", "job_id": job.id}


# ── Search ──

@router.post("/search/reindex")
async def reindex_search(db: AsyncSession = Depends(get_db)):
    from app.services.search import SearchService
    svc = SearchService(db)
    return await svc.reindex()


# ── gallery-dl Config ──

class PixivSourceConfig(BaseModel):
    refresh_token: str | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None
    include: str | None = "artworks"
    tags: str | None = "japanese"
    ugoira: str | None = "zip"
    sleep_request: float | None = None
    max_posts: int | None = None
    metadata: bool | None = None
    metadata_bookmark: bool | None = None
    captions: bool | None = None
    comments: bool | None = None
    sanity: bool | None = None

class TwitterSourceConfig(BaseModel):
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None
    include: str | None = "timeline"
    retweets: bool | None = False
    replies: bool | None = False
    cards: bool | None = True
    videos: bool | None = True
    text_tweets: bool | None = False
    quoted: bool | None = False
    pinned: bool | None = None
    previews: bool | None = None
    articles: bool | None = None
    max_posts: int | None = None

class IwaraSourceConfig(BaseModel):
    cookies_path: str | None = None
    cookie_content: str | None = None
    username: str | None = None
    password: str | None = None
    filename: str | None = None
    directory: str | None = None
    format: str | None = None
    include: str | None = None

class DanbooruSourceConfig(BaseModel):
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    favorite_artists: str | None = None
    favorite_tags: str | None = None
    external: bool | None = None
    ugoira: bool | None = None
    metadata: bool | None = None
    filename: str | None = None
    directory: str | None = None


class PinterestSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    domain: str | None = None
    stories: bool | None = True
    videos: bool | None = True
    sections: bool | None = True
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None


class LofterSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None


class WeiboSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    videos: bool | None = None
    retweets: bool | None = None
    gifs: bool | None = None
    livephoto: bool | None = None
    movies: bool | None = None
    text: bool | None = None
    include: str | None = None
    filename: str | None = None
    directory: str | None = None


class BilibiliSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    livephoto: bool | None = None
    sleep_request: str | None = None
    filename: str | None = None
    directory: str | None = None


class GalleryDLMultiConfig(BaseModel):
    pixiv: PixivSourceConfig | None = None
    twitter: TwitterSourceConfig | None = None
    iwara: IwaraSourceConfig | None = None
    danbooru: DanbooruSourceConfig | None = None
    pinterest: PinterestSourceConfig | None = None
    lofter: LofterSourceConfig | None = None
    weibo: WeiboSourceConfig | None = None
    bilibili: BilibiliSourceConfig | None = None

GALLERYDL_SOURCE_OPTIONS = {
    "pixiv": {
        "name": "Pixiv",
        "supported": True,
        "description": "Pixiv illustrations, manga, and ugoira.",
    },
    "twitter": {
        "name": "X / Twitter",
        "supported": True,
        "description": "Twitter/X timeline, media, and tweets.",
    },
    "iwara": {
        "name": "Iwara",
        "supported": True,
        "description": "Iwara.tv videos, images, playlists, and user profiles. Requires gallery-dl >= 1.32.",
    },
    "danbooru": {
        "name": "Danbooru",
        "supported": True,
        "description": "Danbooru post download and reference — artist tracking, tag monitoring, metadata enrichment.",
    },
    "pinterest": {
        "name": "Pinterest",
        "supported": True,
        "description": "Pinterest pins, boards, and user profiles. No authentication required — public API only.",
    },
    "lofter": {
        "name": "LOFTER",
        "supported": True,
        "description": "LOFTER blog posts and images. Chinese platform. No authentication required.",
    },
    "weibo": {
        "name": "微博 (Weibo)",
        "supported": True,
        "description": "Weibo user feeds, albums, and statuses. Chinese platform. Cookies optional.",
    },
    "bilibili": {
        "name": "哔哩哔哩 (Bilibili)",
        "supported": True,
        "description": "Bilibili articles and user galleries. No authentication required for public content.",
    },
}

PIXIV_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "refresh_token": "refresh-token", "cookies_path": "cookies",
    "filename": "filename", "directory": "directory",
    "include": "include", "tags": "tags", "ugoira": "ugoira",
    "sleep_request": "sleep-request", "max_posts": "max-posts",
    "metadata": "metadata", "metadata_bookmark": "metadata-bookmark",
    "captions": "captions", "comments": "comments", "sanity": "sanity",
}

TWITTER_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory", "include": "include",
    "retweets": "retweets", "replies": "replies",
    "cards": "cards", "videos": "videos",
    "text_tweets": "text-tweets", "quoted": "quoted",
    "pinned": "pinned", "previews": "previews", "articles": "articles",
    "max_posts": "max-posts",
}

IWARA_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "username": "username",
    "password": "password", "filename": "filename",
    "directory": "directory", "format": "format",
    "include": "include",
}

DANBOORU_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "username": "username", "password": "password",
    "api_key": "api-key", "cookies_path": "cookies",
    "favorite_artists": "favorite-artists", "favorite_tags": "favorite-tags",
    "external": "external", "ugoira": "ugoira", "metadata": "metadata",
    "filename": "filename", "directory": "directory",
}


PINTEREST_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "domain": "domain", "stories": "stories",
    "videos": "videos", "sections": "sections",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory",
}

LOFTER_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory",
}

WEIBO_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies",
    "videos": "videos",
    "retweets": "retweets",
    "gifs": "gifs",
    "livephoto": "livephoto",
    "movies": "movies",
    "text": "text",
    "include": "include",
    "filename": "filename",
    "directory": "directory",
}

BILIBILI_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "livephoto": "livephoto",
    "sleep_request": "sleep-request",
    "filename": "filename",
    "directory": "directory",
}

def _load_config() -> tuple[dict, str]:
    import json, os
    config_path = os.path.join(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")
    config = {}
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    return config, config_path

def _read_source_config(extractor_config: dict, schema_map: dict) -> dict:
    """Read gallery-dl extractor config back into our API schema shape."""
    result = {}
    for api_key, dl_key in schema_map.items():
        val = extractor_config.get(dl_key)
        if api_key == "directory" and isinstance(val, list):
            val = "/".join(val)
        if api_key == "ugoira" and isinstance(val, bool):
            val = "zip"
        if val is not None:
            result[api_key] = val
    return result

def _write_source_config(extractor_config: dict, data: BaseModel, schema_map: dict, dir_keys: set = None) -> dict:
    """Merge API schema fields back into gallery-dl extractor config."""
    if dir_keys is None:
        dir_keys = {"directory"}
    for api_key, dl_key in schema_map.items():
        val = getattr(data, api_key, None)
        if val is not None:
            if dl_key in dir_keys and isinstance(val, str) and "/" in val:
                extractor_config[dl_key] = val.split("/")
            elif isinstance(val, str) and val == "":
                # Remove empty strings — they break gallery-dl defaults
                extractor_config.pop(dl_key, None)
            else:
                extractor_config[dl_key] = val
    return extractor_config


@router.get("/gallerydl-config")
async def get_gallerydl_config(source: str | None = None):
    """Get gallery-dl config for one or all sources. Pass ?source=pixiv|twitter|iwara."""
    config, _ = _load_config()
    extractors = config.get("extractor", {})

    def get_source(source_name, schema_map):
        src = extractors.get(source_name, {})
        return _read_source_config(src, schema_map)

    all_config = {
        "pixiv": get_source("pixiv", PIXIV_CONFIG_MAP),
        "twitter": get_source("twitter", TWITTER_CONFIG_MAP),
        "iwara": get_source("iwara", IWARA_CONFIG_MAP),
        "danbooru": get_source("danbooru", DANBOORU_CONFIG_MAP),
        "pinterest": get_source("pinterest", PINTEREST_CONFIG_MAP),
        "lofter": get_source("lofter", LOFTER_CONFIG_MAP),
        "weibo": get_source("weibo", WEIBO_CONFIG_MAP),
        "bilibili": get_source("bilibili", BILIBILI_CONFIG_MAP),
        "sources": GALLERYDL_SOURCE_OPTIONS,
    }
    if source:
        return {source: all_config.get(source, {}), "sources": GALLERYDL_SOURCE_OPTIONS}
    return all_config


@router.put("/gallerydl-config")
async def update_gallerydl_config(data: GalleryDLMultiConfig):
    """Update gallery-dl extractor configs. Only provided source blocks are updated."""
    import json, os
    config, config_path = _load_config()

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    extractors = config.setdefault("extractor", {})

    # Write cookie content to files (auto-sets cookies_path if not provided)
    cookies_dir = os.path.join(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "cookies")
    for source_name, source_data, source_map in [
        ("pixiv", data.pixiv, PIXIV_CONFIG_MAP),
        ("twitter", data.twitter, TWITTER_CONFIG_MAP),
        ("iwara", data.iwara, IWARA_CONFIG_MAP),
        ("danbooru", data.danbooru, DANBOORU_CONFIG_MAP),
        ("pinterest", data.pinterest, PINTEREST_CONFIG_MAP),
        ("lofter", data.lofter, LOFTER_CONFIG_MAP),
        ("weibo", data.weibo, WEIBO_CONFIG_MAP),
        ("bilibili", data.bilibili, BILIBILI_CONFIG_MAP),
    ]:
        if source_data is None:
            continue
        cc = getattr(source_data, "cookie_content", None)
        if cc and cc.strip():
            os.makedirs(cookies_dir, exist_ok=True)
            cookie_path = os.path.join(cookies_dir, f"{source_name}.txt")
            with open(cookie_path, "w") as f:
                f.write(cc.strip() + "\n")
            # Auto-set cookies_path if not explicitly provided
            if not getattr(source_data, "cookies_path", None):
                source_data.cookies_path = f"/gallerydl-config/cookies/{source_name}.txt"

    if data.pixiv is not None:
        pixiv = extractors.setdefault("pixiv", {})
        _write_source_config(pixiv, data.pixiv, PIXIV_CONFIG_MAP)
        extractors["pixiv"] = pixiv

    if data.twitter is not None:
        twitter = extractors.setdefault("twitter", {})
        _write_source_config(twitter, data.twitter, TWITTER_CONFIG_MAP)
        extractors["twitter"] = twitter

    if data.iwara is not None:
        iwara = extractors.setdefault("iwara", {})
        _write_source_config(iwara, data.iwara, IWARA_CONFIG_MAP)
        extractors["iwara"] = iwara

    if data.danbooru is not None:
        danbooru = extractors.setdefault("danbooru", {})
        _write_source_config(danbooru, data.danbooru, DANBOORU_CONFIG_MAP)
        extractors["danbooru"] = danbooru

    if data.pinterest is not None:
        pinterest = extractors.setdefault("pinterest", {})
        _write_source_config(pinterest, data.pinterest, PINTEREST_CONFIG_MAP)
        extractors["pinterest"] = pinterest

    if data.lofter is not None:
        lofter = extractors.setdefault("lofter", {})
        _write_source_config(lofter, data.lofter, LOFTER_CONFIG_MAP)
        extractors["lofter"] = lofter

    if data.weibo is not None:
        weibo = extractors.setdefault("weibo", {})
        _write_source_config(weibo, data.weibo, WEIBO_CONFIG_MAP)
        extractors["weibo"] = weibo

    if data.bilibili is not None:
        bilibili = extractors.setdefault("bilibili", {})
        _write_source_config(bilibili, data.bilibili, BILIBILI_CONFIG_MAP)
        extractors["bilibili"] = bilibili

    config["extractor"] = extractors

    # Ensure metadata postprocessor is always present (for import pipeline)
    config.setdefault("postprocessors", [])
    pp_list = config["postprocessors"]
    # Remove old metadata entries
    pp_list[:] = [p for p in pp_list if p.get("name") not in ("ugoira", "metadata")]
    # Add metadata PP (always, for import JSONs)
    pp_list.append({
        "name": "metadata",
        "event": "after",
        "filename": "{filename}.json",
    })

    # Configure ugoira postprocessor based on Pixiv ugoira format
    if data.pixiv is not None and data.pixiv.ugoira is not None:
        ugoira = data.pixiv.ugoira
        if ugoira in ("gif", "webm", "mp4"):
            pp_list.insert(0, {
                "name": "ugoira",
                "extension": ugoira,
                "keep-files": False,
            })

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return {"status": "ok", "message": "gallery-dl config updated", "path": config_path}


# ── Dedup ──

@router.get("/dedup/duplicates")
async def list_duplicates():
    from app.database import async_session
    from app.models import WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(
                WorkSource.source,
                WorkSource.source_work_id,
                func.count(WorkSource.id).label("cnt"),
                func.array_agg(WorkSource.work_id).label("work_ids"),
            )
            .group_by(WorkSource.source, WorkSource.source_work_id)
            .having(func.count(WorkSource.id) > 1)
            .limit(100)
        )
        duplicates = []
        for row in result:
            duplicates.append({
                "source": row.source,
                "source_work_id": row.source_work_id,
                "count": row.cnt,
                "work_ids": [str(w) for w in row.work_ids],
            })
        return {"duplicates": duplicates, "total": len(duplicates)}


@router.post("/dedup/scan")
async def scan_duplicates():
    from app.database import async_session
    from app.models import WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(func.count(func.distinct(WorkSource.source + WorkSource.source_work_id)).label("unique_works"),
                   func.count(WorkSource.id).label("total_sources"))
        )
        row = result.one()
        return {
            "status": "ok",
            "unique_works": row.unique_works,
            "total_source_records": row.total_sources,
            "message": "Dedup scan completed.",
        }


# ── Merge Candidates ──

@router.get("/merge-candidates")
async def list_merge_candidates():
    from app.database import async_session
    from app.models import Work, WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(
                Work.title,
                func.count(func.distinct(WorkSource.source)).label("source_count"),
                func.array_agg(func.distinct(WorkSource.source)).label("sources"),
                func.array_agg(Work.id).label("work_ids"),
            )
            .join(WorkSource, WorkSource.work_id == Work.id)
            .where(Work.title.isnot(None))
            .group_by(Work.title)
            .having(func.count(func.distinct(WorkSource.source)) > 1)
            .limit(100)
        )
        candidates = []
        for row in result:
            candidates.append({
                "title": row.title,
                "source_count": row.source_count,
                "sources": [str(s) for s in row.sources],
                "work_ids": [str(w) for w in row.work_ids],
            })
        return {"candidates": candidates, "total": len(candidates)}


@router.post("/merge-candidates/{work_id}/merge")
async def merge_works(work_id: str, data: dict):
    return {"status": "not_implemented", "message": "Merge functionality is deferred to a future phase."}


# ── Auth Status ──

@router.get("/auth-status")
async def get_auth_status(db: AsyncSession = Depends(get_db)):
    """List auth health for all subscription sources."""
    from app.models import SubscriptionSource, Subscription, Creator
    from sqlalchemy import select as s

    result = await db.execute(
        s(SubscriptionSource, Subscription, Creator)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .join(Creator, Creator.id == Subscription.creator_id)
        .order_by(SubscriptionSource.auth_healthy.asc())
    )
    sources = []
    for ss, sub, creator in result:
        sources.append({
            "id": str(ss.id),
            "source": ss.source,
            "source_url": ss.source_url,
            "source_creator_id": ss.source_creator_id,
            "auth_healthy": ss.auth_healthy,
            "last_successful_auth": ss.last_successful_auth.isoformat() if ss.last_successful_auth else None,
            "is_enabled": ss.is_enabled,
            "subscription": {
                "id": str(sub.id),
                "name": sub.name,
                "is_active": sub.is_active,
                "sync_enabled": sub.sync_enabled,
            },
            "creator": {
                "id": str(creator.id),
                "name": creator.name,
                "display_name": creator.display_name,
            },
        })
    healthy = sum(1 for s in sources if s["auth_healthy"] is True)
    unhealthy = sum(1 for s in sources if s["auth_healthy"] is False)
    unknown = sum(1 for s in sources if s["auth_healthy"] is None)
    return {
        "sources": sources,
        "summary": {"total": len(sources), "healthy": healthy, "unhealthy": unhealthy, "unknown": unknown},
    }


# ── Backup & Restore ──

BACKUP_DIR = Path(settings.download_root) / ".backups"


def _parse_db_url(url: str) -> dict:
    """Parse DATABASE_URL into pg_dump-compatible components."""
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "postgres",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "autogallery",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/") or "autogallery",
    }


@router.post("/backup")
async def create_backup():
    """Create a full system backup (DB dump + configs + download archives).

    Returns the backup filename and size. Download via GET /admin/backup/download.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"auto-gallery-backup_{ts}.tar.gz"
    filepath = BACKUP_DIR / filename

    db = _parse_db_url(settings.database_url)
    tmpdir = tempfile.mkdtemp(prefix="ag-backup-")

    try:
        # 1. PostgreSQL dump
        dump_path = os.path.join(tmpdir, "database.sql")
        env = os.environ.copy()
        env["PGPASSWORD"] = db["password"]
        result = subprocess.run(
            ["pg_dump", "-h", db["host"], "-p", db["port"], "-U", db["user"],
             "-d", db["dbname"], "--no-owner", "--no-acl", "-f", dump_path],
            capture_output=True, text=True, env=env, timeout=120,
        )
        if result.returncode != 0:
            logger.error("pg_dump failed: %s", result.stderr)
            raise RuntimeError(f"Database dump failed: {result.stderr[:500]}")

        # 2. Config files
        config_src = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"))
        config_dst = os.path.join(tmpdir, "gallerydl-config")
        if config_src.exists():
            shutil.copytree(str(config_src), config_dst, symlinks=False, ignore_dangling_symlinks=True,
                            ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".git"))

        app_config_src = Path(os.environ.get("APP_CONFIG_ROOT", "/app-config"))
        app_config_dst = os.path.join(tmpdir, "app-config")
        if app_config_src.exists():
            shutil.copytree(str(app_config_src), app_config_dst, symlinks=False, ignore_dangling_symlinks=True,
                            ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".git"))

        # 3. Download archives
        dl_root = Path(settings.download_root)
        archives_dst = os.path.join(tmpdir, "download-archives")
        os.makedirs(archives_dst, exist_ok=True)
        for af in dl_root.glob("archive-*.sqlite3"):
            shutil.copy2(str(af), os.path.join(archives_dst, af.name))

        # 4. Backup manifest
        manifest = {
            "created_at": ts,
            "version": "0.1.0",
            "contents": ["database", "gallerydl-config", "app-config", "download-archives"],
        }
        with open(os.path.join(tmpdir, "manifest.json"), "w") as f:
            import json
            json.dump(manifest, f, indent=2)

        # Create tar.gz
        with tarfile.open(filepath, "w:gz") as tar:
            for item in os.listdir(tmpdir):
                tar.add(os.path.join(tmpdir, item), arcname=item)

        file_size = os.path.getsize(filepath)
        logger.info("Backup created: %s (%.1f MB)", filename, file_size / 1024 / 1024)

        # Clean up old backups (keep last 5)
        existing = sorted(BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"))
        for old in existing[:-5]:
            old.unlink()
            logger.info("Removed old backup: %s", old.name)

        return {
            "status": "ok",
            "filename": filename,
            "size_bytes": file_size,
            "size_mb": round(file_size / 1024 / 1024, 1),
        }

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get("/backup/download")
async def download_backup(filename: str | None = None):
    """Download a backup file. If filename not specified, returns the latest."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"))
    if not existing:
        return {"status": "error", "message": "No backups available"}

    target = None
    if filename:
        target = BACKUP_DIR / filename
    else:
        target = existing[-1]

    if not target.exists():
        return {"status": "error", "message": f"Backup not found: {filename}"}

    return FileResponse(
        str(target),
        media_type="application/gzip",
        filename=target.name,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


@router.get("/backup/list")
async def list_backups():
    """List available backup files."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"), reverse=True)
    result = []
    for f in existing:
        stat = f.stat()
        result.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"backups": result}


@router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Restore system from a backup file. THIS IS DESTRUCTIVE — replaces current data."""
    if not file.filename or not file.filename.endswith(".tar.gz"):
        return {"status": "error", "message": "Invalid file: must be a .tar.gz backup"}

    tmpdir = tempfile.mkdtemp(prefix="ag-restore-")
    try:
        # Save uploaded file
        upload_path = os.path.join(tmpdir, file.filename)
        with open(upload_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Extract
        extract_dir = os.path.join(tmpdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(upload_path, "r:gz") as tar:
            tar.extractall(extract_dir)

        # Verify manifest
        manifest_path = os.path.join(extract_dir, "manifest.json")
        if not os.path.exists(manifest_path):
            return {"status": "error", "message": "Invalid backup: no manifest.json found"}

        results = []

        # 1. Restore database
        dump_path = os.path.join(extract_dir, "database.sql")
        if os.path.exists(dump_path):
            db = _parse_db_url(settings.database_url)
            env = os.environ.copy()
            env["PGPASSWORD"] = db["password"]
            # Drop and recreate
            result = subprocess.run(
                ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["dbname"],
                 "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
                capture_output=True, text=True, env=env, timeout=30,
            )
            if result.returncode != 0:
                logger.error("Schema reset failed: %s", result.stderr)
                results.append({"item": "database", "status": "error", "error": result.stderr[:200]})
            else:
                result = subprocess.run(
                    ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["dbname"],
                     "-f", dump_path],
                    capture_output=True, text=True, env=env, timeout=120,
                )
                if result.returncode == 0:
                    results.append({"item": "database", "status": "restored"})
                    logger.info("Database restored from backup")
                else:
                    logger.error("DB restore failed: %s", result.stderr)
                    results.append({"item": "database", "status": "error", "error": result.stderr[:200]})

        # 2. Restore config files
        for src_name, dst_env in [("gallerydl-config", "GALLERYDL_CONFIG_ROOT"),
                                   ("app-config", "APP_CONFIG_ROOT")]:
            src = os.path.join(extract_dir, src_name)
            if os.path.exists(src):
                dst = os.environ.get(dst_env, f"/{src_name}")
                if os.path.exists(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
                results.append({"item": src_name, "status": "restored"})

        # 3. Restore download archives
        archives_src = os.path.join(extract_dir, "download-archives")
        if os.path.exists(archives_src):
            dl_root = str(settings.download_root)
            for af in os.listdir(archives_src):
                shutil.copy2(os.path.join(archives_src, af), os.path.join(dl_root, af))
            results.append({"item": "download-archives", "status": "restored"})

        return {"status": "ok", "results": results}

    except Exception as e:
        logger.exception("Restore failed")
        return {"status": "error", "message": str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
