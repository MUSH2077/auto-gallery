import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_HOURS = 6
DEFAULT_SCAN_MINUTES = 60
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 600
DEFAULT_STALL_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 60
DEFAULT_GALLERYDL_RETRIES = 3
DEFAULT_GALLERYDL_TIMEOUT = 30
DEFAULT_GALLERYDL_ABORT = 5

GALLERYDL_METADATA_POSTPROCESSOR = {
    "name": "metadata",
    "event": "after",
    "filename": "{filename}.json",
}

DEFAULT_GALLERYDL_CONFIG = {
    "extractor": {
        "pixiv": {
            "auto-enable-on-import": True,
            "include": "artworks",
            "tags": "japanese",
            "ugoira": "zip",
            "metadata": False,
            "metadata-bookmark": False,
            "captions": False,
            "comments": False,
            "sanity": False,
            "directory": ["pixiv", "{user[account]}", "{id}"],
            "filename": "{id}_p{num}.{extension}",
        },
        "twitter": {
            "auto-enable-on-import": False,
            "strategy": "tweets",
            "include": "timeline",
            "retweets": False,
            "replies": False,
            "cards": True,
            "videos": True,
            "text-tweets": False,
            "quoted": False,
            "pinned": False,
            "previews": False,
            "articles": False,
            "directory": ["twitter", "{user[name]}"],
            "filename": "{tweet_id}_{num}.{extension}",
        },
        "iwara": {
            "auto-enable-on-import": False,
            "directory": ["iwara", "{user[name]}"],
            "filename": "{date} {id} {title[:200]} {filename}.{extension}",
        },
        "danbooru": {
            "auto-enable-on-import": False,
            "external": False,
            "metadata": False,
            "directory": ["danbooru", "{artist[name]}"],
            "filename": "{id}_{num}.{extension}",
        },
        "pinterest": {
            "auto-enable-on-import": False,
            "stories": True,
            "videos": True,
            "sections": True,
            "directory": ["pinterest", "{category}", "{user}", "{board[name]}"],
            "filename": "{id}_{num}.{extension}",
        },
        "lofter": {
            "auto-enable-on-import": False,
            "directory": ["lofter", "{blog_name}", "{id}"],
            "filename": "{id}_{num}.{extension}",
        },
        "weibo": {
            "auto-enable-on-import": False,
            "videos": True,
            "retweets": False,
            "gifs": True,
            "livephoto": False,
            "movies": False,
            "text": False,
            "directory": ["weibo", "{user[screen_name]}"],
            "filename": "{id}_{num}.{extension}",
        },
        "bilibili": {
            "auto-enable-on-import": False,
            "livephoto": True,
            "sleep-request": "3.0-6.0",
            "directory": ["bilibili", "{user[name]}", "{id}"],
            "filename": "{id}_{num}.{extension}",
        },
    },
    "postprocessors": [GALLERYDL_METADATA_POSTPROCESSOR],
}

EXTRACTOR_SOURCE_ALIASES = {
    "twitter": "x",
}

SOURCE_EXTRACTOR_ALIASES = {
    "x": "twitter",
}


async def get_system_setting(db: AsyncSession, key: str, default: dict | None = None) -> dict:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row and isinstance(row.value, dict):
        return dict(row.value)
    return dict(default or {})


async def get_subscription_defaults(db: AsyncSession) -> dict:
    defaults = await get_system_setting(db, "subscription_defaults")
    return {
        "sync_interval_hours": int(defaults.get("default_sync_interval_hours", DEFAULT_SYNC_INTERVAL_HOURS)),
        "sync_enabled": True,
        "is_active": True,
        "schedule_mode": None,
        "scheduled_times": None,
    }


async def get_scheduler_config(db: AsyncSession) -> dict:
    return await get_system_setting(db, "subscription_defaults")


async def get_download_defaults(db: AsyncSession) -> dict:
    defaults = await get_system_setting(db, "download_defaults")
    return {
        "timeout_seconds": int(defaults.get("timeout_seconds", DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)),
        "stall_timeout_seconds": int(defaults.get("stall_timeout_seconds", DEFAULT_STALL_TIMEOUT_SECONDS)),
        "max_retries": int(defaults.get("max_retries", DEFAULT_MAX_RETRIES)),
        "retry_backoff_base_seconds": int(defaults.get("retry_backoff_base_seconds", DEFAULT_BACKOFF_BASE_SECONDS)),
        "gallerydl_retries": int(defaults.get("gallerydl_retries", DEFAULT_GALLERYDL_RETRIES)),
        "gallerydl_timeout": int(defaults.get("gallerydl_timeout", DEFAULT_GALLERYDL_TIMEOUT)),
        "gallerydl_abort": int(defaults.get("gallerydl_abort", DEFAULT_GALLERYDL_ABORT)),
        **defaults,
    }


def extractor_key_for_source(source: str) -> str:
    return SOURCE_EXTRACTOR_ALIASES.get(source, source)


def source_key_for_extractor(extractor: str) -> str:
    return EXTRACTOR_SOURCE_ALIASES.get(extractor, extractor)


def gallerydl_config_path() -> Path:
    root = os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config")
    return Path(root) / "config.json"


def default_gallerydl_config() -> dict:
    return copy.deepcopy(DEFAULT_GALLERYDL_CONFIG)


def _read_gallerydl_config(path: Path | None = None) -> dict:
    path = path or gallerydl_config_path()
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Failed to load gallery-dl config from %s", path, exc_info=True)
        return {}


def _ensure_metadata_postprocessor(config: dict) -> None:
    postprocessors = config.get("postprocessors")
    if not isinstance(postprocessors, list):
        config["postprocessors"] = [copy.deepcopy(GALLERYDL_METADATA_POSTPROCESSOR)]
        return
    if not any(isinstance(pp, dict) and pp.get("name") == "metadata" for pp in postprocessors):
        postprocessors.append(copy.deepcopy(GALLERYDL_METADATA_POSTPROCESSOR))


def apply_gallerydl_defaults(config: dict | None) -> tuple[dict, bool]:
    """Fill missing gallery-dl defaults without overwriting user settings."""
    original = copy.deepcopy(config) if isinstance(config, dict) else {}
    merged = _deep_merge_missing(original, DEFAULT_GALLERYDL_CONFIG)
    _ensure_metadata_postprocessor(merged)
    return merged, merged != original


def write_gallerydl_config(config: dict, path: Path | None = None) -> None:
    path = path or gallerydl_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_stat = path.stat()
        mode = existing_stat.st_mode & 0o777
        uid = existing_stat.st_uid
        gid = existing_stat.st_gid
    except FileNotFoundError:
        parent_stat = path.parent.stat()
        mode = 0o600
        uid = parent_stat.st_uid
        gid = parent_stat.st_gid
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(config, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    try:
        os.chmod(tmp_path, mode)
        os.chown(tmp_path, uid, gid)
    except PermissionError:
        logger.debug("Could not preserve ownership for gallery-dl config %s", path, exc_info=True)
    os.replace(tmp_path, path)


def ensure_gallerydl_config(path: Path | None = None) -> dict:
    path = path or gallerydl_config_path()
    config = _read_gallerydl_config(path)
    config, changed = apply_gallerydl_defaults(config)
    if changed or not path.exists():
        write_gallerydl_config(config, path)
        logger.info("Ensured gallery-dl config defaults at %s", path)
    return config


def load_gallerydl_config() -> dict:
    return ensure_gallerydl_config()


def _deep_merge_missing(base: Any, fallback: Any) -> Any:
    """Return base with missing values filled from fallback; base/user wins."""
    if not isinstance(base, dict) or not isinstance(fallback, dict):
        return copy.deepcopy(base) if base is not None else copy.deepcopy(fallback)

    merged = copy.deepcopy(base)
    for key, value in fallback.items():
        if key not in merged or merged[key] is None:
            merged[key] = copy.deepcopy(value)
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge_missing(merged[key], value)
    return merged


def merge_gallerydl_effective_config(
    source: str,
    provider_config: dict | None,
    user_config: dict | None,
) -> dict:
    """Build a per-job config where user settings win over provider defaults.

    Provider config supplies safe fallback values such as default cookie paths.
    The saved gallery-dl config is treated as user intent and is never overwritten
    by provider defaults.
    """
    extractor_key = extractor_key_for_source(source)
    provider_extractors = (provider_config or {}).get("extractor", {})
    provider_source_cfg = provider_extractors.get(extractor_key, {})

    user_extractors = (user_config or {}).get("extractor", {})
    user_source_cfg = user_extractors.get(extractor_key, {})

    source_cfg = _deep_merge_missing(user_source_cfg, provider_source_cfg)

    effective: dict[str, Any] = {"extractor": {extractor_key: source_cfg}}
    for key in ("postprocessors", "output", "downloader"):
        if key in (user_config or {}):
            effective[key] = copy.deepcopy(user_config[key])
    return effective


def build_effective_gallerydl_config(
    source: str,
    provider_config: dict | None,
    user_config: dict | None = None,
) -> dict:
    if user_config is None:
        user_config = load_gallerydl_config()
    return merge_gallerydl_effective_config(
        source=source,
        provider_config=provider_config,
        user_config=user_config,
    )
