"""Explicit workload descriptions consumed by the resource worker.

The descriptor is a small, serializable contract carried in RQ metadata.  The
legacy function-name mapping remains isolated here for old jobs; the worker
itself only consumes descriptors and never interprets business arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ResourceDescriptor:
    name: str
    profile: str
    child_profile: str | None = None
    nonblocking_child: bool = False
    bind_slice: Callable[[tuple[Any, ...], int, float | None], tuple[Any, ...]] | None = None
    default_units: int = 1


def _fixed(args: tuple[Any, ...], units: int, seconds: float | None) -> tuple[Any, ...]:
    del args, seconds
    return (units,)


def _bounded_media(args: tuple[Any, ...], units: int, seconds: float | None) -> tuple[Any, ...]:
    current = float(args[1]) if len(args) > 1 else 20.0
    return (1, min(current, seconds) if seconds is not None else current)


def _bounded_units(default: int, args: tuple[Any, ...], units: int, seconds: float | None) -> tuple[Any, ...]:
    current_units = int(args[0]) if args else default
    current_seconds = float(args[1]) if len(args) > 1 else 20.0
    return (min(current_units, units), min(current_seconds, seconds) if seconds is not None else current_seconds)


def _import_slice(args, units, seconds):
    return _bounded_units(25, args, units, seconds)


def _git_slice(args, units, seconds):
    return _bounded_units(1, args, units, seconds)


def _search_slice(args, units, seconds):
    del seconds
    current = int(args[0]) if args else 500
    return (min(current, units),)


DESCRIPTORS = {
    "media-derivatives": ResourceDescriptor("media-derivatives", "image_derive", "image_derive", True, _bounded_media),
    "asset-dedup-outbox": ResourceDescriptor("asset-dedup-outbox", "image_derive", "image_derive", True, lambda a, u, s: _fixed(a, 1, s)),
    "import-projection": ResourceDescriptor("import-projection", "import_db", "import_db", True, _import_slice, 25),
    "gitllery-projection": ResourceDescriptor("gitllery-projection", "git_projection", "git_projection", True, _git_slice, 1),
    "search-projection": ResourceDescriptor("search-projection", "pg_search_db", "pg_search_db", True, _search_slice, 500),
    "maintenance": ResourceDescriptor("maintenance", "maintenance"),
    # The parent owns admission for the entire DB-only heat recomputation.
    "work-heat": ResourceDescriptor("work-heat", "work_heat"),
    "git-sync": ResourceDescriptor("git-sync", "git_projection", "git_projection"),
    "import-maintenance": ResourceDescriptor("import-maintenance", "import_db", "import_db"),
    "search-maintenance": ResourceDescriptor("search-maintenance", "search_index", "search_index"),
    "maintenance-child": ResourceDescriptor("maintenance-child", "maintenance", "maintenance"),
    "dedup-scan": ResourceDescriptor("dedup-scan", "image_derive", "image_derive", nonblocking_child=True),
}


LEGACY_FUNCTION_DESCRIPTORS = {
    "run_work_heat_recompute": "work-heat",
    "run_gitllery_sync_operation": "git-sync",
    "run_curation_backfill_operation": "import-maintenance",
    "run_search_reindex_operation": "search-maintenance",
    "import_runner.run_import_job": "import-maintenance",
    "run_clear_operation": "maintenance-child",
    "run_library_rebuild_operation": "maintenance-child",
    "run_disk_import_operation": "maintenance-child",
    "run_creator_reenrich_operation": "maintenance-child",
    "run_gitllery_rebuild_operation": "maintenance-child",
    "run_asset_dedup_scan": "dedup-scan",
    "media_derivatives": "media-derivatives",
    "run_asset_dedup_outbox": "asset-dedup-outbox",
    "import_projection": "import-projection",
    "gitllery_projection": "gitllery-projection",
    "search_projection": "search-projection",
}


def descriptor_by_name(name: str | None) -> ResourceDescriptor | None:
    return DESCRIPTORS.get(str(name)) if name else None


def descriptor_for_job(job) -> ResourceDescriptor | None:
    metadata = getattr(job, "meta", None) or {}
    explicit = descriptor_by_name(metadata.get("resource_descriptor"))
    if explicit:
        return explicit
    # Compatibility adapter for persisted RQ jobs created before descriptors.
    func_name = str(getattr(job, "func_name", "") or "").casefold()
    for marker, name in LEGACY_FUNCTION_DESCRIPTORS.items():
        if marker in func_name:
            return DESCRIPTORS[name]
    return None


def descriptor_for_workload(workload: str) -> ResourceDescriptor | None:
    return next((item for item in DESCRIPTORS.values() if item.profile == workload), None)
