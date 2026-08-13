"""Capability-based NAS performance envelopes.

The profile only caps background throughput. It never changes whether a
subscription is due, so a compact NAS drains the same durable backlog more
slowly instead of silently skipping work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

GIB = 1024 ** 3


@dataclass(frozen=True)
class DevicePerformanceProfile:
    name: str
    scheduler_publish_limit: int
    download_queue_limit: int
    download_concurrency_limit: int
    detected_memory_bytes: int | None
    detected_cpu_count: int | None
    source: str


PROFILE_LIMITS = {
    "compact": (5, 25, 1),
    "standard": (25, 100, 1),
    "performance": (50, 200, 2),
}


def _memory_total_bytes(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        return None
    return None


def classify_device_profile(
    memory_total_bytes: int | None,
    cpu_count: int | None,
) -> str:
    """Classify by capabilities, failing safe when metrics are unavailable."""

    if memory_total_bytes is None or cpu_count is None:
        return "compact"
    if memory_total_bytes < 12 * GIB or cpu_count < 6:
        return "compact"
    if memory_total_bytes >= 24 * GIB and cpu_count >= 8:
        return "performance"
    return "standard"


def resolve_device_profile(
    configured: str | None = None,
    *,
    memory_total_bytes: int | None = None,
    cpu_count: int | None = None,
) -> DevicePerformanceProfile:
    requested = str(configured or "auto").strip().lower()
    detected_memory = _memory_total_bytes() if memory_total_bytes is None else memory_total_bytes
    detected_cpus = os.cpu_count() if cpu_count is None else cpu_count
    if requested in PROFILE_LIMITS:
        name = requested
        source = "configured"
    else:
        name = classify_device_profile(detected_memory, detected_cpus)
        source = "capability_auto" if requested == "auto" else "invalid_fallback_auto"
    publish, queued, concurrency = PROFILE_LIMITS[name]
    return DevicePerformanceProfile(
        name=name,
        scheduler_publish_limit=publish,
        download_queue_limit=queued,
        download_concurrency_limit=concurrency,
        detected_memory_bytes=detected_memory,
        detected_cpu_count=detected_cpus,
        source=source,
    )


@lru_cache(maxsize=1)
def current_device_profile() -> DevicePerformanceProfile:
    # Import lazily so the worker supervisor can use the pure helpers at boot.
    from app.config import settings

    return resolve_device_profile(settings.resource_device_profile)
