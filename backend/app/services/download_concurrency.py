"""Shared, side-effect-free download concurrency resolution."""

from __future__ import annotations

from dataclasses import dataclass
import os

MIN_DOWNLOAD_CONCURRENCY = 1
MAX_DOWNLOAD_CONCURRENCY = 5
DEFAULT_DOWNLOAD_CONCURRENCY = 3
DEFAULT_DOWNLOAD_CONCURRENCY_CAP = 1


def _clamp(value: object, *, default: int, lo: int = MIN_DOWNLOAD_CONCURRENCY,
           hi: int = MAX_DOWNLOAD_CONCURRENCY) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def resolve_concurrency(db_value, argv_value, default=DEFAULT_DOWNLOAD_CONCURRENCY,
                        lo=MIN_DOWNLOAD_CONCURRENCY, hi=MAX_DOWNLOAD_CONCURRENCY) -> int:
    """Pick the persisted setting, then CLI fallback, then the default."""
    for candidate in (db_value, argv_value, default):
        if candidate is None:
            continue
        try:
            number = int(candidate)
        except (TypeError, ValueError):
            continue
        return max(lo, min(hi, number))
    return default


def resolve_concurrency_cap(value=None, *, lo=MIN_DOWNLOAD_CONCURRENCY,
                            hi=MAX_DOWNLOAD_CONCURRENCY) -> int:
    """Parse the deployment cap, defaulting safely to one download."""
    candidate = value if value is not None else os.environ.get(
        "DOWNLOAD_CONCURRENCY_CAP", str(DEFAULT_DOWNLOAD_CONCURRENCY_CAP)
    )
    return _clamp(candidate, default=DEFAULT_DOWNLOAD_CONCURRENCY_CAP, lo=lo, hi=hi)


def resolve_optional_device_limit(value) -> int | None:
    """Return an explicit per-download device limit; empty means automatic."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _clamp(value, default=DEFAULT_DOWNLOAD_CONCURRENCY_CAP)


def resolve_effective_download_concurrency(configured: int, cap: int, device_limit: int) -> int:
    """Calculate the actual number of download execution slots."""
    return min(
        _clamp(configured, default=DEFAULT_DOWNLOAD_CONCURRENCY),
        _clamp(cap, default=DEFAULT_DOWNLOAD_CONCURRENCY_CAP),
        _clamp(device_limit, default=DEFAULT_DOWNLOAD_CONCURRENCY_CAP),
    )


_UNSET = object()


def execution_limit(
    configured: int | None = None,
    cap: int | None = None,
    automatic_device_limit: int | None = None,
    explicit_device_limit=_UNSET,
) -> int:
    """Return the runtime download-slot limit used by heavy I/O admission.

    With no arguments, read the running Settings and capability profile. The
    individual arguments remain available for side-effect-free unit tests and
    callers that already resolved those values. Missing values fail safe to
    one, and an explicit device override affects downloads only.
    """
    if configured is None or cap is None or automatic_device_limit is None or explicit_device_limit is _UNSET:
        from app.config import settings
        from app.services.device_profile import current_device_profile

        configured = settings.download_execution_concurrency if configured is None else configured
        cap = settings.download_concurrency_cap if cap is None else cap
        automatic_device_limit = (
            current_device_profile().download_concurrency_limit
            if automatic_device_limit is None
            else automatic_device_limit
        )
        if explicit_device_limit is _UNSET:
            explicit_device_limit = settings.download_device_concurrency_limit
    device_limit = resolve_optional_device_limit(explicit_device_limit)
    return resolve_effective_download_concurrency(
        configured if configured is not None else 1,
        cap if cap is not None else DEFAULT_DOWNLOAD_CONCURRENCY_CAP,
        device_limit or automatic_device_limit or DEFAULT_DOWNLOAD_CONCURRENCY_CAP,
    )


@dataclass(frozen=True)
class DownloadConcurrencyDecision:
    configured: int
    cap: int
    effective: int
    automatic_device_limit: int
    explicit_device_limit: int | None

    @property
    def device_limit(self) -> int:
        return self.explicit_device_limit or self.automatic_device_limit


def resolve_download_concurrency(
    db_value,
    argv_value,
    *,
    cap_value=None,
    automatic_device_limit: int = DEFAULT_DOWNLOAD_CONCURRENCY_CAP,
    explicit_device_limit=None,
    default: int = DEFAULT_DOWNLOAD_CONCURRENCY,
) -> DownloadConcurrencyDecision:
    """Resolve configured, capped and effective download concurrency."""
    configured = resolve_concurrency(db_value, argv_value, default=default)
    cap = resolve_concurrency_cap(cap_value)
    automatic = _clamp(automatic_device_limit, default=DEFAULT_DOWNLOAD_CONCURRENCY_CAP)
    explicit = resolve_optional_device_limit(explicit_device_limit)
    device_limit = explicit or automatic
    effective = resolve_effective_download_concurrency(configured, cap, device_limit)
    return DownloadConcurrencyDecision(
        configured=configured,
        cap=min(cap, device_limit),
        effective=effective,
        automatic_device_limit=automatic,
        explicit_device_limit=explicit,
    )