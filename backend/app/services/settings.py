import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_HOURS = 6
DEFAULT_SCAN_MINUTES = 60
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 600
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 60

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
        "max_retries": int(defaults.get("max_retries", DEFAULT_MAX_RETRIES)),
        "retry_backoff_base_seconds": int(defaults.get("retry_backoff_base_seconds", DEFAULT_BACKOFF_BASE_SECONDS)),
        **defaults,
    }


def extractor_key_for_source(source: str) -> str:
    return SOURCE_EXTRACTOR_ALIASES.get(source, source)


def source_key_for_extractor(extractor: str) -> str:
    return EXTRACTOR_SOURCE_ALIASES.get(extractor, extractor)


def gallerydl_config_path() -> Path:
    root = os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config")
    return Path(root) / "config.json"


def load_gallerydl_config() -> dict:
    path = gallerydl_config_path()
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Failed to load gallery-dl config from %s", path, exc_info=True)
        return {}


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


def _normalize_directory(value: Any) -> Any:
    if isinstance(value, str) and "/" in value:
        return [part for part in value.split("/") if part]
    return value


def merge_gallerydl_effective_config(
    source: str,
    provider_config: dict | None,
    user_config: dict | None,
    naming_template: str | None = None,
) -> dict:
    """Build a per-job config where user settings win over provider defaults.

    Provider config supplies safe fallback values such as default cookie paths.
    The saved gallery-dl config is treated as user intent and is never overwritten
    by provider defaults. A selected naming template is also user-managed and wins
    for the source directory.
    """
    extractor_key = extractor_key_for_source(source)
    provider_extractors = (provider_config or {}).get("extractor", {})
    provider_source_cfg = provider_extractors.get(extractor_key, {})

    user_extractors = (user_config or {}).get("extractor", {})
    user_source_cfg = user_extractors.get(extractor_key, {})

    source_cfg = _deep_merge_missing(user_source_cfg, provider_source_cfg)
    if naming_template:
        source_cfg["directory"] = _normalize_directory(naming_template)

    effective: dict[str, Any] = {"extractor": {extractor_key: source_cfg}}
    for key in ("postprocessors", "output", "downloader"):
        if key in (user_config or {}):
            effective[key] = copy.deepcopy(user_config[key])
    return effective


def build_effective_gallerydl_config(
    source: str,
    provider_config: dict | None,
    naming_template: str | None = None,
    user_config: dict | None = None,
) -> dict:
    if user_config is None:
        user_config = load_gallerydl_config()
    return merge_gallerydl_effective_config(
        source=source,
        provider_config=provider_config,
        user_config=user_config,
        naming_template=naming_template,
    )
