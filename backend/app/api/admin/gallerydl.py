"""Gallery-dl config management."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.services.redis_client import get_redis

from ._routers import router

class PixivSourceConfig(BaseModel):
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
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
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None
    strategy: str | None = "tweets"
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
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    username: str | None = None
    password: str | None = None
    filename: str | None = None
    directory: str | None = None
    format: str | None = None
    include: str | None = None

class DanbooruSourceConfig(BaseModel):
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    external: bool | None = None
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
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "refresh_token": "refresh-token", "cookies_path": "cookies",
    "filename": "filename", "directory": "directory",
    "include": "include", "tags": "tags", "ugoira": "ugoira",
    "sleep_request": "sleep-request", "max_posts": "max-posts",
    "metadata": "metadata", "metadata_bookmark": "metadata-bookmark",
    "captions": "captions", "comments": "comments", "sanity": "sanity",
}

TWITTER_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory", "strategy": "strategy", "include": "include",
    "retweets": "retweets", "replies": "replies",
    "cards": "cards", "videos": "videos",
    "text_tweets": "text-tweets", "quoted": "quoted",
    "pinned": "pinned", "previews": "previews", "articles": "articles",
    "max_posts": "max-posts",
}

IWARA_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "username": "username",
    "password": "password", "filename": "filename",
    "directory": "directory", "format": "format",
    "include": "include",
}

DANBOORU_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "username": "username", "password": "password",
    "api_key": "api-key", "cookies_path": "cookies",
        "external": "external", "metadata": "metadata",
    "filename": "filename", "directory": "directory",
}


PINTEREST_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "domain": "domain", "stories": "stories",
    "videos": "videos", "sections": "sections",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory",
}

LOFTER_CONFIG_MAP = {
    "cookie_content": "cookies-content",
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

def _convert_to_netscape_cookies(raw: str, source: str) -> str:
    """Convert browser-pasted cookies to Netscape format that gallery-dl expects.

    Accepts:
    - Header format: ``name1=value1; name2=value2`` (one line)
    - Line format: ``name=value`` (one per line)
    - Netscape format: already 7 tab-separated fields (returned as-is)

    Returns Netscape-formatted cookie content.
    """
    # If already Netscape format (tab-separated, 7 fields), return as-is
    lines = [l.strip() for l in raw.split("\n") if l.strip() and not l.startswith("#")]
    if lines:
        first = lines[0]
        if "\t" in first and len(first.split("\t")) >= 6:
            return raw
        # Check if it's header-style (contains "; " semicolon separators)
        if "; " in raw or ";" in raw:
            # Header format: split by ";" into individual cookies
            cookies = []
            for part in raw.replace("; ", ";").split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, value = part.partition("=")
                    cookies.append((name.strip(), value.strip()))
        else:
            # One cookie per line: name=value
            cookies = []
            for line in lines:
                if "=" in line:
                    name, _, value = line.partition("=")
                    cookies.append((name.strip(), value.strip()))
    else:
        return raw

    if not cookies:
        return raw

    # Map source to appropriate domain
    domain_map = {
        "twitter": ".twitter.com",
        "x": ".x.com",
        "pixiv": ".pixiv.net",
        "danbooru": ".danbooru.donmai.us",
        "iwara": ".iwara.tv",
        "pinterest": ".pinterest.com",
        "lofter": ".lofter.com",
        "weibo": ".weibo.com",
        "bilibili": ".bilibili.com",
    }
    domain = domain_map.get(source, f".{source}.com")
    far_future = "9999999999"

    out = ["# Netscape HTTP Cookie File"]
    for name, value in cookies:
        out.append(f"{domain}\tTRUE\t/\tFALSE\t{far_future}\t{name}\t{value}")

    logger.info("Converted %d cookies to Netscape format for %s", len(cookies), source)
    return "\n".join(out)


def _load_config() -> tuple[dict, str]:
    from app.services.settings import ensure_gallerydl_config, gallerydl_config_path

    config_path = gallerydl_config_path()
    return ensure_gallerydl_config(config_path), str(config_path)

def _read_source_config(extractor_config: dict, schema_map: dict, config: dict | None = None) -> dict:
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


def _rebuild_managed_postprocessors(config: dict, pixiv_ugoira: str | None) -> list[dict]:
    """Rebuild auto-gallery managed gallery-dl postprocessors.

    The metadata postprocessor is required by the import pipeline. The ugoira
    postprocessor is controlled by the current Pixiv config and must survive
    saves from other provider tabs.

    When ugoira format conversion is enabled (gif/webm/mp4), this mirrors the
    behavior of gallery-dl's --ugoira CLI flag:
    1. Sets extractor.pixiv.ugoira to "original" so the extractor downloads
       individual frames instead of a ZIP archive
    2. Adds a ugoira postprocessor to assemble frames into the chosen format
    """
    postprocessors = config.setdefault("postprocessors", [])
    postprocessors[:] = [
        pp for pp in postprocessors
        if not (isinstance(pp, dict) and pp.get("name") in ("ugoira", "metadata"))
    ]

    if pixiv_ugoira in ("gif", "webm", "mp4"):
        postprocessors.append({
            "name": "ugoira",
            "extension": pixiv_ugoira,
            "keep-files": False,
        })

    postprocessors.append({
        "name": "metadata",
        "event": "after",
        "filename": "{filename}.json",
    })
    return postprocessors


# ── Gallery-dl connectivity test ──

TEST_URLS = {
    "pixiv": "https://www.pixiv.net/users/11",
    "twitter": "https://x.com/X",
    "iwara": "https://www.iwara.tv/videos",
    "danbooru": "https://danbooru.donmai.us/posts?tags=rating:s&limit=1",
    "pinterest": "https://www.pinterest.com/pinterest/",
    "lofter": "https://www.lofter.com/tag/%E6%8F%92%E7%94%BB",
    "weibo": "https://weibo.com/u/2803301701",
    "bilibili": "https://space.bilibili.com/8047632",
}

AUTH_ERROR_PATTERNS = [
    (r"(?i)AuthRequired", "Authentication failed — invalid or missing credentials"),
    (r"(?i)AuthorizationError", "Authorization denied — check account permissions"),
    (r"(?i)HttpError.*401", "HTTP 401 Unauthorized — check username/password"),
    (r"(?i)HttpError.*403", "HTTP 403 Forbidden — check account access"),
    (r"(?i)LoginRequired", "Login required — credentials not provided or invalid"),
    (r"(?i)NotFoundError", "User/page not found — check the test URL"),
    (r"(?i)cookie.*(?:expired|invalid|missing)", "Cookie expired or invalid"),
    (r"(?i)token.*(?:expired|invalid|revoked)", "Token expired or revoked"),
    (r"(?i)Could not authenticate", "Authentication rejected by server"),
]

@router.post("/gallerydl-config/test-connection")
async def test_source_connection(data: dict):
    """Test gallery-dl connectivity for a given source using current credentials."""
    source = data.get("source", "")
    if source not in TEST_URLS:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    test_url = TEST_URLS[source]
    import json as _json, os, re as _re, subprocess, tempfile, shutil

    tmpdir = tempfile.mkdtemp(prefix="gallerydl-test-")
    tmp_config = os.path.join(tmpdir, "test-config.json")
    try:
        from app.providers import registry as _registry
        from app.services.settings import (
            build_effective_gallerydl_config,
            source_key_for_extractor,
        )

        provider_source = source_key_for_extractor(source)
        provider_config = {}
        try:
            provider = _registry.get(provider_source)
            provider_config = provider.build_gallerydl_config(None)
        except KeyError:
            provider_config = {}
        config = build_effective_gallerydl_config(provider_source, provider_config)
        with open(tmp_config, "w") as f:
            _json.dump(config, f)

        cmd = [
            "gallery-dl", "--config", tmp_config,
            "--write-metadata",
            "--range", "1-1",
            "--destination", tmpdir,
            test_url,
        ]

        env = os.environ.copy()
        proxy_enabled = False
        try:
            from app.services.proxy import _load_proxy_config, get_proxy_env
            proxy_config = await _load_proxy_config()
            proxy_enabled = bool(proxy_config.get("enabled", False))
            env.update(get_proxy_env(proxy_config))
        except Exception:
            logger.warning("Failed to apply proxy env for gallery-dl test", exc_info=True)

        logger.info("Testing %s connectivity: %s (proxy=%s)", source, " ".join(cmd), proxy_enabled)
        # gallery-dl can run up to 120s — off the event loop so a connection
        # test doesn't freeze the whole backend for other users.
        result = await asyncio.to_thread(
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env))
        stderr = result.stderr or ""
        stdout = result.stdout or ""
        combined = stdout + stderr

        auth_issue = None
        for pattern, label in AUTH_ERROR_PATTERNS:
            if _re.search(pattern, combined):
                auth_issue = label
                break

        if result.returncode == 0 and not auth_issue:
            return {
                "source": source, "success": True,
                "message": f"Connection test passed for {source}. Credentials are valid.",
                "details": stderr[:1000] or "(no output)",
            }
        elif auth_issue:
            return {
                "source": source, "success": False,
                "message": auth_issue,
                "details": (stderr or stdout or "")[:2000],
            }
        else:
            # Show last portion of stderr (gallery-dl puts the actual error at the end)
            detail = (stderr or stdout or "").strip()
            if len(detail) > 2000:
                detail = "...(truncated)\n" + detail[-2000:]
            return {
                "source": source, "success": False,
                "message": f"gallery-dl exited with code {result.returncode}",
                "details": detail or "(no output — check gallery-dl version and network connectivity)",
            }

    except subprocess.TimeoutExpired:
        return {
            "source": source, "success": False,
            "message": "Connection test timed out after 120s",
            "details": "The request to the remote server took too long.",
        }
    except Exception:
        logger.exception("Connection test failed for %s", source)
        return {
            "source": source, "success": False,
            "message": "Connection test failed unexpectedly.",
            "details": "Check the backend logs for the request details.",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get("/gallerydl-config/effective")
async def get_effective_gallerydl_config(
    source: str,
    subscription_source_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Preview the effective per-job gallery-dl config used by workers."""
    from uuid import UUID

    from app.models.subscription_source import SubscriptionSource
    from app.providers import registry as _registry
    from app.services.job_manifest import redacted_manifest_config
    from app.services.settings import (
        build_effective_gallerydl_config,
        extractor_key_for_source,
        source_key_for_extractor,
    )

    provider_source = source_key_for_extractor(source)
    source_url = None
    if subscription_source_id:
        ss = await db.get(SubscriptionSource, UUID(subscription_source_id))
        if not ss:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        provider_source = ss.source
        source_url = ss.source_url

    try:
        provider = _registry.get(provider_source)
    except KeyError:
        raise HTTPException(status_code=400, detail={
            "code": "unknown_provider",
            "message": f"Unknown source provider: {provider_source}",
            "retryable": False,
        })

    provider_config = provider.build_gallerydl_config(None)
    effective = build_effective_gallerydl_config(provider_source, provider_config)

    url_valid = bool(source_url and provider.validate_url(provider.normalize_url(source_url) or source_url)) if source_url else None
    return {
        "source": provider_source,
        "extractor": extractor_key_for_source(provider_source),
        "source_url": source_url,
        "url_valid": url_valid,
        "config": redacted_manifest_config(effective),
    }


@router.get("/gallerydl-config")
async def get_gallerydl_config(source: str | None = None):
    """Get gallery-dl config for one or all sources. Pass ?source=pixiv|twitter|iwara."""
    config, _ = _load_config()
    extractors = config.get("extractor", {})

    def get_source(source_name, schema_map):
        src = extractors.get(source_name, {})
        return _read_source_config(src, schema_map, config)

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
    # Read cookie file contents for sources that have cookies configured
    for src_name, src_cfg in all_config.items():
        if isinstance(src_cfg, dict) and src_cfg.get("cookies_path"):
            cookie_path = os.path.join(
                os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"),
                src_cfg["cookies_path"].lstrip("/"))
            if os.path.exists(cookie_path):
                try:
                    with open(cookie_path) as cf:
                        src_cfg["cookie_content"] = cf.read().strip()
                except Exception:
                    pass

    if source:
        return {source: all_config.get(source, {}), "sources": GALLERYDL_SOURCE_OPTIONS}
    return all_config


@router.put("/gallerydl-config")
async def update_gallerydl_config(data: GalleryDLMultiConfig):
    """Update gallery-dl extractor configs. Only provided source blocks are updated."""
    import os
    from app.services.settings import write_gallerydl_config

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
            content = _convert_to_netscape_cookies(cc.strip(), source_name)
            os.makedirs(cookies_dir, exist_ok=True)
            cookie_path = os.path.join(cookies_dir, f"{source_name}.txt")
            with open(cookie_path, "w") as f:
                f.write(content + "\n")
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

    # Ensure managed postprocessors match the final saved config. This keeps
    # Pixiv GIF conversion stable even when saving a non-Pixiv provider tab.
    pixiv_ugoira = extractors.get("pixiv", {}).get("ugoira")
    _rebuild_managed_postprocessors(config, pixiv_ugoira)

    write_gallerydl_config(config, Path(config_path))
    return {"status": "ok", "message": "gallery-dl config updated", "path": config_path}


# ── Dedup ──
