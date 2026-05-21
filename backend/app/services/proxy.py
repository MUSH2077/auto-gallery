"""Read proxy settings from system_settings and apply to urllib."""

import logging
import urllib.request

from app.config import settings as app_settings

logger = logging.getLogger(__name__)

_cached_proxy: dict | None = None


def get_cached_proxy_config() -> dict:
    """Return cached proxy config, loading from DB if needed.

    Uses a dedicated asyncio event loop + asyncpg connection so it can be
    called from any thread (including asyncio.to_thread workers) without
    conflicting with the main event loop or SQLAlchemy session.
    """
    global _cached_proxy
    if _cached_proxy is not None:
        return _cached_proxy

    try:
        import asyncio
        import asyncpg
        import json
        from urllib.parse import urlparse

        db_url = app_settings.database_url
        parsed = urlparse(db_url)
        dsn = (
            f"postgresql://{parsed.username}:{parsed.password}"
            f"@{parsed.hostname}:{parsed.port or 5432}/{parsed.path.lstrip('/')}"
        )

        async def _query():
            conn = await asyncpg.connect(dsn)
            try:
                row = await conn.fetchrow(
                    "SELECT value FROM system_settings WHERE key = 'proxy'"
                )
                if row:
                    val = row["value"]
                    if isinstance(val, str):
                        val = json.loads(val)
                    return val
            finally:
                await conn.close()

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_query())
            if result:
                logger.debug("Proxy config loaded: enabled=%s http=%s",
                           result.get("enabled"), result.get("http_proxy", "not set"))
                _cached_proxy = result
        finally:
            loop.close()
    except Exception as e:
        logger.warning("Failed to load proxy config: %s", e)
        _cached_proxy = {}
    return _cached_proxy or {}


async def _load_proxy_config() -> dict:
    """Read proxy settings from the database (async version for API use)."""
    try:
        from app.database import async_session
        from app.models.system_setting import SystemSetting
        from sqlalchemy import select

        async with async_session() as db:
            result = await db.execute(
                select(SystemSetting).where(SystemSetting.key == "proxy")
            )
            row = result.scalar_one_or_none()
            if row and row.value:
                logger.debug("Proxy config loaded: enabled=%s http=%s",
                           row.value.get("enabled"), row.value.get("http_proxy", "not set"))
                global _cached_proxy
                _cached_proxy = row.value
                return row.value
    except Exception as e:
        logger.warning("Failed to load proxy config: %s", e)
    return {}


def apply_proxy_to_urllib(config: dict) -> urllib.request.OpenerDirector | None:
    """Create a urllib opener with proxy settings. Returns None if proxy is disabled."""
    if not config.get("enabled"):
        return None

    proxies = {}
    http_proxy = config.get("http_proxy", "")
    https_proxy = config.get("https_proxy", "")

    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    if not proxies:
        return None

    no_proxy = config.get("no_proxy", "")
    handler = urllib.request.ProxyHandler(proxies)
    opener = urllib.request.build_opener(handler)

    logger.info("Using proxy: http=%s https=%s no_proxy=%s", http_proxy, https_proxy, no_proxy)
    return opener


def get_proxy_env(config: dict) -> dict[str, str]:
    """Return proxy env vars for subprocess (gallery-dl)."""
    if not config.get("enabled"):
        return {}
    env = {}
    http_proxy = config.get("http_proxy", "")
    https_proxy = config.get("https_proxy", "")
    no_proxy = config.get("no_proxy", "")
    if http_proxy:
        env["HTTP_PROXY"] = http_proxy
        env["http_proxy"] = http_proxy
    if https_proxy:
        env["HTTPS_PROXY"] = https_proxy
        env["https_proxy"] = https_proxy
    if no_proxy:
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    return env
