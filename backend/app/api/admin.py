import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    default_sync_enabled: bool = True
    schedule_mode: str = "interval"
    scheduled_times: str = ""

DEFAULT_SUB = {"default_sync_interval_hours": 6, "scheduler_scan_interval_minutes": 60, "default_sync_enabled": True, "schedule_mode": "interval", "scheduled_times": ""}

class DownloadDefaults(BaseModel):
    timeout_seconds: int = 600
    max_retries: int = 3
    retry_backoff_base_seconds: int = 60
    max_posts: int = 200

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


DEFAULT_DEDUP = {"source_level_enabled": False, "cross_source_enabled": False, "auto_merge": False, "phash_threshold": 8}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200}


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
        _clear_files(["/downloads", "/library"])
        results["files"] = "downloads + library cleared"
        return {"status": "ok", "message": "All data cleared", "deleted": results}

    tables = ENTITIES.get(entity)
    if tables is None:
        raise HTTPException(status_code=400, detail=f"Unknown entity: {entity}. Valid: all, works, creators, downloads")

    results = {}
    for table in tables:
        r = await db.execute(text(f"DELETE FROM {table}"))
        results[table] = r.rowcount
    await db.commit()

    # Clear files for works-related entities
    if entity == "works":
        _clear_files(["/downloads", "/library"])
        results["files"] = "downloads + library cleared"

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
    max_posts: int | None = None

class IwaraSourceConfig(BaseModel):
    cookies_path: str | None = None
    cookie_content: str | None = None
    username: str | None = None
    password: str | None = None
    filename: str | None = None
    directory: str | None = None
    format: str | None = None

class GalleryDLMultiConfig(BaseModel):
    pixiv: PixivSourceConfig | None = None
    twitter: TwitterSourceConfig | None = None
    iwara: IwaraSourceConfig | None = None

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
}

PIXIV_CONFIG_MAP = {
    "refresh_token": "refresh-token", "cookies_path": "cookies",
    "filename": "filename", "directory": "directory",
    "include": "include", "tags": "tags", "ugoira": "ugoira",
    "sleep_request": "sleep-request", "max_posts": "max-posts",
}

TWITTER_CONFIG_MAP = {
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory", "include": "include",
    "retweets": "retweets", "replies": "replies",
    "cards": "cards", "videos": "videos",
    "text_tweets": "text-tweets", "quoted": "quoted",
    "max_posts": "max-posts",
}

IWARA_CONFIG_MAP = {
    "cookies_path": "cookies", "username": "username",
    "password": "password", "filename": "filename",
    "directory": "directory", "format": "format",
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
        "filename": "{id}_p{num}.{extension}.json",
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
