"""Process-local storage read-model cache shared by its HTTP and job adapters."""
import time

_value: dict | None = None
_stored_at = 0.0
_TTL_SECONDS = 60.0


def cached_storage_breakdown() -> dict | None:
    if _value is not None and time.monotonic() - _stored_at < _TTL_SECONDS:
        return _value
    return None


def cache_storage_breakdown(value: dict) -> None:
    global _value, _stored_at
    _value, _stored_at = value, time.monotonic()


def invalidate_storage_breakdown_cache() -> None:
    global _value, _stored_at
    _value, _stored_at = None, 0.0
