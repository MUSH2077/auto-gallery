"""Read proxy settings from system_settings and apply to urllib."""

import logging
import urllib.request

logger = logging.getLogger(__name__)

_cached_proxy: dict | None = None


async def _load_proxy_config() -> dict:
    """Read proxy settings from the database."""
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
