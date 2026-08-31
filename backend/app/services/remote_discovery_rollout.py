"""Backend-authoritative rollout gates for private remote-follow discovery."""

from __future__ import annotations

from typing import Any

from app.config import settings


REMOTE_DISCOVERY_SOURCES = ("pixiv", "x", "bilibili")


class RemoteDiscoveryUnavailable(RuntimeError):
    """A provider operation is outside the effective deployment rollout."""

    def __init__(self, code: str, source: str | None = None):
        self.code = code
        self.source = source
        super().__init__(f"Remote discovery is unavailable ({code})")


def _provider_flags(config: Any, source: str) -> tuple[bool, bool]:
    if source == "pixiv":
        return (
            bool(config.remote_discovery_pixiv_preview_enabled),
            bool(config.remote_discovery_pixiv_auto_import_enabled),
        )
    if source == "x":
        return (
            bool(config.remote_discovery_x_enabled),
            bool(config.remote_discovery_x_auto_import_enabled),
        )
    if source == "bilibili":
        return (
            bool(config.remote_discovery_bilibili_enabled),
            bool(config.remote_discovery_bilibili_auto_import_enabled),
        )
    raise ValueError("Unsupported remote discovery source")


def provider_rollout(source: str, config: Any = settings) -> dict[str, Any]:
    """Return effective capabilities after closing dependency gaps."""

    preview_flag, auto_flag = _provider_flags(config, source)
    foundation = bool(config.remote_discovery_private_members_enabled)
    preview = foundation and preview_flag
    automatic = preview and auto_flag
    if not foundation:
        reason = "private_members_disabled"
    elif not preview_flag:
        reason = f"{source}_preview_disabled"
    elif not auto_flag:
        reason = "auto_import_disabled" if source == "pixiv" else "auto_import_not_rolled_out"
    else:
        reason = None
    return {
        "manual_preview": preview,
        "auto_import": automatic,
        "unavailable_reason": reason,
    }


def rollout_capabilities(config: Any = settings) -> dict[str, Any]:
    return {
        "private_members": bool(config.remote_discovery_private_members_enabled),
        "providers": {
            source: provider_rollout(source, config)
            for source in REMOTE_DISCOVERY_SOURCES
        },
    }


def preview_enabled(source: str, config: Any = settings) -> bool:
    return bool(provider_rollout(source, config)["manual_preview"])


def auto_import_enabled(source: str, config: Any = settings) -> bool:
    return bool(provider_rollout(source, config)["auto_import"])


def enabled_preview_sources(config: Any = settings) -> tuple[str, ...]:
    return tuple(source for source in REMOTE_DISCOVERY_SOURCES if preview_enabled(source, config))


def require_preview(source: str, config: Any = settings) -> None:
    capability = provider_rollout(source, config)
    if not capability["manual_preview"]:
        raise RemoteDiscoveryUnavailable(str(capability["unavailable_reason"]), source)


def require_auto_import(source: str, config: Any = settings) -> None:
    capability = provider_rollout(source, config)
    if not capability["auto_import"]:
        reason = capability["unavailable_reason"] or "auto_import_disabled"
        raise RemoteDiscoveryUnavailable(str(reason), source)
