"""System info, storage, memory, settings, proxy, integrity check, reset."""

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
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.models.system_setting import SystemSetting
from app.models.subscription_source import SubscriptionSource
from app.schemas.gitllery import GitllerySettingsResponse
from app.services.redis_client import get_redis
from app.services.queue_admission import (
    QueueAdmissionError,
    checked_enqueue_in,
)
from app.services import admin_data
from app.services.admin_data import ENTITIES, clear_entity_data

from ._routers import router


def _job_is_sync_scan(job) -> bool:
    return "sync_subscriptions" in (getattr(job, "func_name", "") or str(job))


def _reschedule_subscription_sync_scan(config: dict) -> dict:
    """Replace pending subscription sync scans with the current interval."""
    from rq import Queue
    from rq.registry import ScheduledJobRegistry

    from app.jobs.subscription_sync import sync_subscriptions

    interval = max(int(config.get("scheduler_scan_interval_minutes", 60)), 5)
    redis = get_redis()
    queue = Queue(name="scheduled", connection=redis)
    scheduled_registry = ScheduledJobRegistry(queue=queue)

    scheduled_old_ids = []
    for job_id in list(scheduled_registry.get_job_ids()):
        job = queue.fetch_job(job_id)
        if job and _job_is_sync_scan(job):
            scheduled_old_ids.append(job_id)

    queued_old_ids = []
    for job in list(queue.get_jobs()):
        if _job_is_sync_scan(job):
            queued_old_ids.append(job.id)

    # Publish first so a late Redis capacity/write failure never deletes the
    # only known-good recurring scan. The replacement is at least five minutes
    # away, leaving time to remove the superseded entries below.
    job = checked_enqueue_in(
        queue,
        timedelta(minutes=interval),
        sync_subscriptions,
    )
    removed = 0
    for old_job_id in scheduled_old_ids:
        if old_job_id != job.id:
            scheduled_registry.remove(old_job_id, delete_job=True)
            removed += 1
    for old_job_id in queued_old_ids:
        if old_job_id != job.id:
            queue.remove(old_job_id)
            removed += 1
    return {"removed": removed, "job_id": job.id, "interval_minutes": interval}


DEFAULT_DEDUP = {
    "auto_group_enabled": True,
    "phash_threshold": 4,
    "ssim_threshold": 0.98,
    "aspect_ratio_tolerance": 0.01,
    "auto_group_score": 95,
    "review_score": 70,
    "quarantine_days": 30,
}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False}

_system_info_cache: dict | None = None
_system_info_cache_ts: float = 0.0
_SYSTEM_INFO_CACHE_TTL = 60.0
_system_info_lock = asyncio.Lock()

class DedupSettings(BaseModel):
    auto_group_enabled: bool = True
    phash_threshold: int = Field(default=4, ge=0, le=4)
    ssim_threshold: float = Field(default=0.98, ge=0.9, le=1.0)
    aspect_ratio_tolerance: float = Field(default=0.01, ge=0.0, le=0.05)
    auto_group_score: float = Field(default=95, ge=70, le=100)
    review_score: float = Field(default=70, ge=0, le=100)
    quarantine_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def validate_score_order(self):
        if self.review_score > self.auto_group_score:
            raise ValueError("review_score must not exceed auto_group_score")
        return self

class SubscriptionDefaults(BaseModel):
    default_sync_interval_hours: int = 6
    scheduler_scan_interval_minutes: int = 60
    scheduler_enabled: bool = True
    schedule_mode: str = "interval"
    scheduled_times: str = ""
    timezone: str = "UTC"
    auto_enable_sources: str = "pixiv"

DEFAULT_SUB = {"default_sync_interval_hours": 6, "scheduler_scan_interval_minutes": 60, "scheduler_enabled": True, "schedule_mode": "interval", "scheduled_times": "", "timezone": "UTC", "auto_enable_sources": "pixiv"}

class DownloadDefaults(BaseModel):
    # extra="allow" preserves fields the frontend sends that aren't declared here
    # (stall_timeout_seconds, gallerydl_*, import_skip_threshold). Without it,
    # saving this page silently dropped those keys back to their defaults.
    model_config = {"extra": "allow"}
    timeout_seconds: int = 600
    max_retries: int = 3
    retry_backoff_base_seconds: int = 60
    max_posts: int = 200
    skip_ai_generated: bool = False
    download_concurrency: int = 3  # parallel download jobs, clamped to 1-5 on read

class ProxySettings(BaseModel):
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1,::1"
    enabled: bool = False

DEFAULT_PROXY = {"http_proxy": "", "https_proxy": "", "no_proxy": "localhost,127.0.0.1,::1", "enabled": False}

class AdminSettingsUpdate(BaseModel):
    dedup: DedupSettings | None = None
    subscription_defaults: SubscriptionDefaults | None = None
    download_defaults: DownloadDefaults | None = None
    proxy: ProxySettings | None = None

async def _get_setting(db: AsyncSession, key: str, default: dict = None) -> dict:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row and isinstance(row.value, dict):
        return {**(default or {}), **row.value}
    return default or {}


async def _put_setting(db: AsyncSession, key: str, value: dict):
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    changed = row is None or row.value != value
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    if key == "subscription_defaults" and changed:
        # Inherited schedules can change for every source.  NULL is the durable
        # invalidation marker consumed fairly by the next 100-row coverage pass.
        await db.execute(
            sql_update(SubscriptionSource).values(next_sync_at=None)
        )
    await db.commit()

    # Invalidate caches when relevant settings change
    if key == "proxy":
        try:
            from app.services.proxy import clear_proxy_cache
            clear_proxy_cache()
        except Exception:
            pass

def _gather_system_info() -> dict:
    """Blocking: disk_usage + recursive directory-size walks. Runs in a thread
    (never on the event loop) — an rglob over a multi-hundred-GB library takes
    seconds to minutes and would freeze all concurrent requests otherwise."""
    info = {"version": "0.1.0", "python": "3.12"}
    for label, path in [("downloads", settings.download_root), ("library", settings.library_root)]:
        try:
            usage = shutil.disk_usage(path)
            info[f"{label}_total_gb"] = round(usage.total / (1024**3), 1)
            info[f"{label}_used_gb"] = round(usage.used / (1024**3), 1)
            info[f"{label}_free_gb"] = round(usage.free / (1024**3), 1)
        except Exception:
            pass
    try:
        dl_root = Path(settings.download_root)
        archives = {}
        for af in dl_root.glob("archive-*.sqlite3"):
            archives[af.stem.replace("archive-", "")] = round(af.stat().st_size / 1024, 1)
        info["archives_kb"] = archives
    except Exception:
        pass
    try:
        for label, path in [("downloads", settings.download_root), ("library", settings.library_root)]:
            total = 0
            for f in Path(path).rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except Exception:
                        pass
            info[f"{label}_size_mb"] = round(total / (1024**2), 1)
    except Exception:
        pass
    return info


@router.get("/system-info")
async def system_info():
    """Return system-level info: disk usage, archive sizes, version.

    Cached (this endpoint is polled by the dashboard) and computed off the
    event loop — the directory-size walk is O(files) over the whole library.
    A single in-flight walk is serialized by a lock so a burst of polls can't
    spawn concurrent multi-GB walks; stale cache is served meanwhile.
    """
    global _system_info_cache, _system_info_cache_ts
    now_mono = time.monotonic()
    if _system_info_cache is not None and (now_mono - _system_info_cache_ts) < _SYSTEM_INFO_CACHE_TTL:
        return _system_info_cache
    if _system_info_lock.locked() and _system_info_cache is not None:
        return _system_info_cache  # another walk in progress — serve stale
    async with _system_info_lock:
        now_mono = time.monotonic()
        if _system_info_cache is not None and (now_mono - _system_info_cache_ts) < _SYSTEM_INFO_CACHE_TTL:
            return _system_info_cache
        info = await asyncio.to_thread(_gather_system_info)
        _system_info_cache = info
        _system_info_cache_ts = time.monotonic()
        return info


def _current_rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB -> MB
    except Exception:
        pass
    return None


@router.get("/memory")
async def memory_diagnostics(top: int = 25):
    """Memory snapshot for OOM diagnosis: process RSS + a census of the most
    common live Python object types. A runaway type count (e.g. millions of
    Work/Row/dict) points straight at what is filling RAM. Cheap — no
    always-on tracemalloc."""
    import gc
    import sys
    from collections import Counter

    def _snapshot() -> dict:
        gc.collect()
        counts: Counter = Counter()
        sizes: Counter = Counter()
        for obj in gc.get_objects():
            try:
                tn = type(obj).__name__
                counts[tn] += 1
                sizes[tn] += sys.getsizeof(obj)
            except Exception:
                continue
        top_by_count = [
            {"type": t, "count": c, "approx_kb": round(sizes[t] / 1024, 1)}
            for t, c in counts.most_common(max(1, min(top, 100)))
        ]
        return {
            "total_tracked_objects": sum(counts.values()),
            "gc_counts": gc.get_count(),
            "top_types": top_by_count,
        }

    snap = await asyncio.to_thread(_snapshot)
    return {
        "rss_mb": _current_rss_mb(),
        "pool": {"size": settings.db_pool_size, "max_overflow": settings.db_max_overflow},
        **snap,
    }


_storage_breakdown_cache: dict | None = None
_storage_breakdown_cache_ts: float = 0.0
_STORAGE_BREAKDOWN_CACHE_TTL = 60.0


def invalidate_storage_breakdown_cache() -> None:
    global _storage_breakdown_cache, _storage_breakdown_cache_ts
    _storage_breakdown_cache = None
    _storage_breakdown_cache_ts = 0.0


@router.get("/storage-breakdown")
async def storage_breakdown(db: AsyncSession = Depends(get_db)):
    """Return per-source and per-creator storage breakdown."""
    global _storage_breakdown_cache, _storage_breakdown_cache_ts
    _now_mono = time.monotonic()
    if _storage_breakdown_cache is not None and (_now_mono - _storage_breakdown_cache_ts) < _STORAGE_BREAKDOWN_CACHE_TTL:
        return _storage_breakdown_cache

    from sqlalchemy import text

    dl_root = Path(settings.download_root)
    lib_root = Path(settings.library_root)

    def _compute_fs_sizes() -> dict:
        """Walk storage once and recover provider-native repository identity."""
        from app.providers import registry
        from app.services.settings import source_key_for_extractor

        def _safe_file_stat(path: Path):
            try:
                return path.stat() if path.is_file() else None
            except Exception:
                return None

        def _safe_file_size(path: Path) -> int:
            stat = _safe_file_stat(path)
            return stat.st_size if stat else 0

        def _safe_dir_size(
            path: Path,
            seen_inodes: set[tuple[int, int]] | None = None,
        ) -> int:
            total = 0
            if seen_inodes is None:
                seen_inodes = set()
            try:
                if not path.exists():
                    return 0
                for f in path.rglob("*"):
                    if f.is_file():
                        stat = _safe_file_stat(f)
                        if not stat:
                            continue
                        inode = (stat.st_dev, stat.st_ino)
                        if inode in seen_inodes:
                            continue
                        seen_inodes.add(inode)
                        total += stat.st_size
            except Exception:
                return total
            return total

        archive_bytes = 0
        try:
            archive_bytes = sum(_safe_file_size(af) for af in dl_root.glob("archive-*.sqlite3"))
        except Exception:
            pass

        backup_bytes = 0
        for backup_root in (dl_root / ".backups", Path(settings.app_config_root) / "backups"):
            backup_bytes += _safe_dir_size(backup_root)

        library_index_bytes = _safe_dir_size(lib_root)
        original_media_bytes = 0
        original_media_inodes: set[tuple[int, int]] = set()
        source_acc: dict[str, dict] = {}
        repositories: list[dict] = []
        try:
            for source_dir in dl_root.iterdir():
                if not source_dir.is_dir():
                    continue
                if source_dir.name == ".dedup-quarantine":
                    original_media_bytes += _safe_dir_size(
                        source_dir,
                        original_media_inodes,
                    )
                    continue
                if source_dir.name.startswith("."):
                    continue
                canonical_source = source_key_for_extractor(source_dir.name)
                try:
                    provider = registry.get(canonical_source)
                except KeyError:
                    original_media_bytes += _safe_dir_size(
                        source_dir,
                        original_media_inodes,
                    )
                    continue

                source_stats = source_acc.setdefault(
                    canonical_source,
                    {
                        "size_bytes": 0,
                        "logical_size_bytes": 0,
                        "creators": set(),
                        "work_ids": set(),
                    },
                )
                for creator_dir in source_dir.iterdir():
                    if not creator_dir.is_dir():
                        continue
                    repo_bytes = 0.0
                    repo_logical_bytes = 0
                    work_ids: set[str] = set()
                    source_creator_id: str | None = None
                    source_url: str | None = None
                    metadata_display_name: str | None = None

                    for file_path in creator_dir.rglob("*"):
                        if not file_path.is_file():
                            continue
                        stat = _safe_file_stat(file_path)
                        if not stat:
                            continue
                        file_size = stat.st_size
                        repo_logical_bytes += file_size
                        # A hard-linked byte has multiple repository paths but
                        # occupies disk once. Attribute an equal share to each
                        # link so creator/repository totals remain meaningful.
                        repo_bytes += file_size / max(1, stat.st_nlink)
                        inode = (stat.st_dev, stat.st_ino)
                        if inode not in original_media_inodes:
                            original_media_inodes.add(inode)
                            original_media_bytes += file_size
                        if file_path.suffix.lower() != ".json":
                            continue
                        try:
                            with file_path.open(encoding="utf-8") as handle:
                                raw = json.load(handle)
                            work_data = provider.parse_work_source(raw)
                            work_id = str(work_data.get("source_work_id") or "").strip()
                            if work_id:
                                work_ids.add(work_id)
                            if source_creator_id is None:
                                creator_data = provider.parse_source_creator(raw)
                                source_creator_id = str(
                                    creator_data.get("source_creator_id") or "",
                                ).strip() or None
                                source_url = creator_data.get("source_url")
                                metadata_display_name = creator_data.get("display_name")
                        except Exception:
                            continue

                    if not work_ids:
                        work_ids = {
                            child.name
                            for child in creator_dir.iterdir()
                            if child.is_dir()
                        }

                    repo_physical_bytes = round(repo_bytes)
                    source_stats["size_bytes"] += repo_physical_bytes
                    source_stats["logical_size_bytes"] += repo_logical_bytes
                    source_stats["creators"].add(creator_dir.name)
                    source_stats["work_ids"].update(work_ids)
                    repositories.append({
                        "disk_source": source_dir.name,
                        "source": canonical_source,
                        "directory_name": creator_dir.name,
                        "size_bytes": repo_physical_bytes,
                        "logical_size_bytes": repo_logical_bytes,
                        "work_count": len(work_ids),
                        "source_creator_id": source_creator_id,
                        "source_url": source_url,
                        "metadata_display_name": metadata_display_name,
                    })
        except Exception:
            logger.warning("Storage breakdown filesystem scan was incomplete", exc_info=True)

        sources = {
            source: {
                "size_mb": round(stats["size_bytes"] / (1024 ** 2), 1),
                "logical_size_mb": round(
                    stats["logical_size_bytes"] / (1024 ** 2),
                    1,
                ),
                "creator_count": len(stats["creators"]),
                "work_count": len(stats["work_ids"]),
            }
            for source, stats in source_acc.items()
        }

        return {
            "archive_bytes": archive_bytes,
            "backup_bytes": backup_bytes,
            "original_media_bytes": original_media_bytes,
            "library_index_bytes": library_index_bytes,
            "sources": sources,
            "repositories": repositories,
        }

    _fs = await asyncio.to_thread(_compute_fs_sizes)
    archive_bytes = _fs["archive_bytes"]
    backup_bytes = _fs["backup_bytes"]
    original_media_bytes = _fs["original_media_bytes"]
    library_index_bytes = _fs["library_index_bytes"]
    sources = _fs["sources"]
    filesystem_repositories = _fs["repositories"]

    # Resolve physical repository directories to stable creator/repository IDs.
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.providers import registry

    repository_contexts = list((await db.execute(
        select(SubscriptionSource, Subscription, Creator)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .join(Creator, Creator.id == Subscription.creator_id)
    )).all())
    source_creators = list((await db.execute(
        select(SourceCreator).where(SourceCreator.creator_id.is_not(None))
    )).scalars().all())
    source_creator_owner = {
        (row.source, row.source_creator_id): str(row.creator_id)
        for row in source_creators
        if row.creator_id
    }

    contexts_by_source: dict[str, list[tuple]] = {}
    creator_by_id: dict[str, Creator] = {}
    for subscription_source, subscription, creator in repository_contexts:
        contexts_by_source.setdefault(subscription_source.source, []).append(
            (subscription_source, subscription, creator),
        )
        creator_by_id[str(creator.id)] = creator

    def _normalized_url(source: str, value: str | None) -> str:
        if not value:
            return ""
        try:
            provider = registry.get(source)
            return (provider.normalize_url(value) or value).rstrip("/").casefold()
        except Exception:
            return value.rstrip("/").casefold()

    creator_nodes: dict[str, dict] = {}
    unlinked_repositories: list[dict] = []
    legacy_creators: list[dict] = []

    for fs_repo in filesystem_repositories:
        source = fs_repo["source"]
        candidates = contexts_by_source.get(source, [])
        owner_id = source_creator_owner.get(
            (source, fs_repo.get("source_creator_id")),
        )
        fs_url = _normalized_url(source, fs_repo.get("source_url"))
        best_context = None
        best_score = 0
        for context in candidates:
            subscription_source, _, creator = context
            score = 0
            if (
                fs_repo.get("source_creator_id")
                and subscription_source.source_creator_id == fs_repo["source_creator_id"]
            ):
                score = max(score, 100)
            if fs_url and _normalized_url(source, subscription_source.source_url) == fs_url:
                score = max(score, 95)
            try:
                provider = registry.get(source)
                url_dir = provider.get_creator_dir_from_url(subscription_source.source_url or "")
                if (
                    url_dir
                    and str(url_dir).casefold() == fs_repo["directory_name"].casefold()
                ):
                    score = max(score, 90)
            except Exception:
                pass
            if owner_id and str(creator.id) == owner_id:
                score = max(score, 80)
            if score > best_score:
                best_score = score
                best_context = context

        creator_id: str | None = owner_id
        repository_id: str | None = None
        display_name = fs_repo.get("metadata_display_name") or fs_repo["directory_name"]
        if best_context:
            subscription_source, _, creator = best_context
            creator_id = str(creator.id)
            repository_id = str(subscription_source.id)
            display_name = creator.display_name or creator.name or display_name
        elif creator_id and creator_id in creator_by_id:
            creator = creator_by_id[creator_id]
            display_name = creator.display_name or creator.name or display_name

        try:
            source_display_name = registry.get(source).display_name
        except Exception:
            source_display_name = source

        child = {
            "repository_id": repository_id,
            "source": source,
            "source_display_name": source_display_name,
            "disk_source": fs_repo["disk_source"],
            "directory_name": fs_repo["directory_name"],
            "size_mb": round(fs_repo["size_bytes"] / (1024 ** 2), 1),
            "logical_size_mb": round(
                fs_repo["logical_size_bytes"] / (1024 ** 2),
                1,
            ),
            "work_count": fs_repo["work_count"],
        }
        legacy_entry = {
            "name": fs_repo["directory_name"],
            "display_name": display_name,
            "source": source,
            "size_mb": child["size_mb"],
            "work_count": child["work_count"],
        }
        if creator_id:
            legacy_entry["creator_id"] = creator_id
        if repository_id:
            legacy_entry["repository_id"] = repository_id
        legacy_creators.append(legacy_entry)

        if not creator_id:
            unlinked_repositories.append(child)
            continue

        node = creator_nodes.setdefault(creator_id, {
            "creator_id": creator_id,
            "display_name": display_name,
            "size_mb": 0.0,
            "work_count": 0,
            "repository_count": 0,
            "repositories": [],
        })
        node["size_mb"] = round(node["size_mb"] + child["size_mb"], 1)
        node["work_count"] += child["work_count"]
        node["repository_count"] += 1
        node["repositories"].append(child)

    creator_tree = sorted(
        creator_nodes.values(),
        key=lambda node: (-node["size_mb"], node["display_name"].casefold()),
    )[:20]
    for node in creator_tree:
        node["repositories"].sort(
            key=lambda child: (-child["size_mb"], child["source"], child["directory_name"]),
        )
    unlinked_repositories.sort(
        key=lambda child: (-child["size_mb"], child["source"], child["directory_name"]),
    )
    creators = sorted(
        legacy_creators,
        key=lambda entry: (-entry["size_mb"], entry["display_name"].casefold()),
    )[:20]

    db_stats = {}
    try:
        for table in ("works", "assets", "creators", "subscriptions", "tags"):
            result = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))
            db_stats[table] = int(result.scalar() or 0)
    except Exception:
        db_stats = {}

    result = {
        "sources": sources,
        "creators": creators,
        "creator_tree": creator_tree,
        "unlinked_repositories": unlinked_repositories,
        "db_stats": db_stats,
        "layers": {
            "original_media_store": {
                "path": str(dl_root),
                "size_mb": round(max(original_media_bytes, 0) / (1024 ** 2), 1),
                "description": "Original media files stored long-term in DOWNLOAD_ROOT.",
            },
            "library_index": {
                "path": str(lib_root),
                "size_mb": round(library_index_bytes / (1024 ** 2), 1),
                "description": "Metadata and thumbnails stored in LIBRARY_ROOT.",
            },
            "download_archives": {
                "path": str(dl_root),
                "size_mb": round(archive_bytes / (1024 ** 2), 1),
                "description": "gallery-dl archive sqlite files used to avoid duplicate downloads.",
            },
            "backups": {
                "path": f"{dl_root / '.backups'}; {Path(settings.app_config_root) / 'backups'}",
                "size_mb": round(backup_bytes / (1024 ** 2), 1),
                "description": "Backup archives created by admin backup tools.",
            },
        },
    }
    _storage_breakdown_cache = result
    _storage_breakdown_cache_ts = time.monotonic()
    return result

@router.get("/integrity-check")
async def integrity_check(db: AsyncSession = Depends(get_db)):
    """Scan for data integrity issues: orphaned files, missing thumbnails, orphaned records."""
    from sqlalchemy import text
    issues = []

    # 1. Orphaned download files (files without work_sources)
    try:
        dl_root = Path(settings.download_root)
        result = await db.execute(text("SELECT source, source_work_id FROM work_sources"))
        db_work_ids = {(row[0], row[1]) for row in result.fetchall()}

        def _scan_orphans() -> list:
            """Blocking full-library walk — offloaded off the event loop."""
            found = []
            for source_dir in dl_root.iterdir():
                if not source_dir.is_dir():
                    continue
                src = source_dir.name
                for creator_dir in source_dir.iterdir():
                    if not creator_dir.is_dir():
                        continue
                    for work_dir in creator_dir.iterdir():
                        if not work_dir.is_dir():
                            continue
                        swid = work_dir.name
                        if (src, swid) not in db_work_ids:
                            file_count = sum(1 for _ in work_dir.rglob("*") if _.is_file())
                            found.append({
                                "path": str(work_dir.relative_to(dl_root)),
                                "source": src,
                                "source_work_id": swid,
                                "file_count": file_count,
                            })
            return found

        orphaned_files = await asyncio.to_thread(_scan_orphans)
        if orphaned_files:
            issues.append({
                "type": "orphaned_download_files",
                "severity": "warning",
                "count": len(orphaned_files),
                "description": "下载目录中存在但数据库无对应 work_source 记录的文件",
                "items": orphaned_files[:50],
            })
    except Exception as e:
        logger.warning("Integrity check - orphaned files: %s", e)

    # 2. Missing thumbnails (works with asset but no thumbnail)
    try:
        result = await db.execute(text(
            "SELECT a.id, a.file_name, ws.source, ws.source_work_id FROM assets a "
            "JOIN work_sources ws ON a.work_id = ws.work_id "
            "WHERE a.thumb_sm_path IS NULL OR a.thumb_sm_path = ''"
        ))
        missing_thumbs = []
        for row in result.fetchall():
            missing_thumbs.append({
                "asset_id": str(row[0]),
                "file_name": row[1],
                "source": row[2],
                "source_work_id": row[3],
            })
        if missing_thumbs:
            issues.append({
                "type": "missing_thumbnails",
                "severity": "warning",
                "count": len(missing_thumbs),
                "description": "有资产记录但缺少缩略图的作品",
                "items": missing_thumbs[:50],
            })
    except Exception as e:
        logger.warning("Integrity check - missing thumbs: %s", e)

    # 3. Orphaned creators (no works, no subscriptions, no source_creators)
    try:
        result = await db.execute(text(
            "SELECT c.id, c.name FROM creators c "
            "LEFT JOIN works w ON w.creator_id = c.id "
            "LEFT JOIN subscriptions s ON s.creator_id = c.id "
            "LEFT JOIN source_creators sc ON sc.creator_id = c.id "
            "WHERE w.id IS NULL AND s.id IS NULL AND sc.id IS NULL"
        ))
        orphaned_creators = [{"id": str(row[0]), "name": row[1]} for row in result.fetchall()]
        if orphaned_creators:
            issues.append({
                "type": "orphaned_creators",
                "severity": "info",
                "count": len(orphaned_creators),
                "description": "无作品、无订阅、无来源账号的孤立创作者",
                "items": orphaned_creators,
            })
    except Exception as e:
        logger.warning("Integrity check - orphaned creators: %s", e)

    # 4. Orphaned tags (no work_tags associations)
    try:
        result = await db.execute(text(
            "SELECT t.id, t.normalized_name FROM tags t "
            "LEFT JOIN work_tags wt ON wt.tag_id = t.id "
            "WHERE wt.tag_id IS NULL"
        ))
        orphaned_tags = [{"id": str(row[0]), "name": row[1]} for row in result.fetchall()]
        if orphaned_tags:
            issues.append({
                "type": "orphaned_tags",
                "severity": "info",
                "count": len(orphaned_tags),
                "description": "无关联作品的孤立标签",
                "items": orphaned_tags,
            })
    except Exception as e:
        logger.warning("Integrity check - orphaned tags: %s", e)

    # 5. Dead links (asset records where file doesn't exist on disk)
    try:
        # Keyset-paged: fetchall over assets is O(rows) resident memory — an
        # OOM vector on a large library. Batches keep memory O(batch).
        dead_links: list[dict] = []
        _DEAD_LINK_BATCH = 5000
        _DEAD_LINK_MAX_ITEMS = 500  # cap the payload; count keeps going
        dead_total = 0
        last_id = None
        while True:
            q = ("SELECT a.id, a.file_path, a.file_name FROM assets a "
                 "WHERE a.file_path IS NOT NULL ")
            params: dict = {"lim": _DEAD_LINK_BATCH}
            if last_id is not None:
                q += "AND a.id > :last_id "
                params["last_id"] = last_id
            q += "ORDER BY a.id LIMIT :lim"
            batch = (await db.execute(text(q), params)).fetchall()
            if not batch:
                break
            last_id = batch[-1][0]

            def _check_batch(rows=batch) -> list:
                found = []
                for row in rows:
                    fpath = row[1]
                    if fpath and not os.path.exists(fpath):
                        found.append({
                            "asset_id": str(row[0]),
                            "file_path": fpath,
                            "file_name": row[2],
                        })
                return found

            batch_dead = await asyncio.to_thread(_check_batch)
            dead_total += len(batch_dead)
            if len(dead_links) < _DEAD_LINK_MAX_ITEMS:
                dead_links.extend(batch_dead[: _DEAD_LINK_MAX_ITEMS - len(dead_links)])
        if dead_total:
            issues.append({
                "type": "dead_links",
                "severity": "error",
                "count": dead_total,
                "description": "数据库记录指向不存在文件的死链",
                "items": dead_links[:50],
            })
    except Exception as e:
        logger.warning("Integrity check - dead links: %s", e)

    # 6. DB table stats
    try:
        tables = ["works", "assets", "creators", "subscriptions", "tags",
                   "download_jobs", "import_jobs", "source_creators", "work_sources"]
        db_stats = {}
        for table in tables:
            r = await db.execute(text(f"SELECT COUNT(*) FROM {table}"))
            db_stats[table] = r.scalar() or 0
    except Exception:
        db_stats = {}

    return {"issues": issues, "db_stats": db_stats, "checked_at": datetime.now(timezone.utc).isoformat()}


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    dedup = await _get_setting(db, "dedup", DEFAULT_DEDUP)
    sub = await _get_setting(db, "subscription_defaults", DEFAULT_SUB)
    dl = await _get_setting(db, "download_defaults", DEFAULT_DL)
    proxy = await _get_setting(db, "proxy", DEFAULT_PROXY)
    return {"dedup": dedup, "subscription_defaults": sub, "download_defaults": dl, "proxy": proxy}


@router.get("/gitllery/settings", response_model=GitllerySettingsResponse)
async def get_gitllery_settings(db: AsyncSession = Depends(get_db)):
    """Return Gitllery v1's deployment-managed, read-only control surface.

    The parent admin router requires the ``system`` permission.  Keep this
    response deliberately declarative: it exposes no environment values,
    filesystem paths, credentials, or mutation controls.
    """

    from app.services.gitllery import GitlleryService

    mode = settings.gitllery_projection_mode.strip().lower()
    projection_mode: Literal["shadow", "active"] = (
        "active" if mode == "active" else "shadow"
    )
    projection_reason = None if projection_mode == "active" else "gitllery_shadow_only"
    projection_capability = {
        "enabled": projection_mode == "active",
        "reason": projection_reason,
    }
    transfer_capability = {
        "enabled": False,
        "reason": (
            "gitllery_shadow_only"
            if projection_mode == "shadow"
            else "gitllery_transfer_not_implemented"
        ),
    }
    status = await GitlleryService(db).status(deep=False)
    return {
        "product_name": "Gitllery",
        "product_version": "v1",
        "format_id": "gitllery-segment",
        "format_revision": 1,
        "projection_mode": projection_mode,
        "build_generation": settings.gitllery_build_generation,
        "managed_by": "deployment_environment",
        "read_only": True,
        "capabilities": {
            "automatic_projection": projection_capability,
            "reconcile": projection_capability,
            "backfill": projection_capability,
            "rebuild": projection_capability,
            "push": transfer_capability,
            "pull": transfer_capability,
            "verify": {"enabled": True, "reason": None},
            "commit": {"enabled": True, "reason": None},
        },
        "cli": {
            "max_works_per_commit": 25,
            "max_operations_per_commit": 100,
            "token_storage": "client_only",
            "server_stores_cli_token": False,
            "examples": {
                "config": "gitllery config set url http://auto-gallery.local",
                "login": "gitllery auth login --username admin",
                "status": "gitllery --remote status",
                "log": "gitllery --remote log --limit 50",
                "verify": "gitllery verify --remote",
                "commit": (
                    "gitllery --remote commit --message \"curate work\" "
                    "work favorite 00000000-0000-0000-0000-000000000001 --set on"
                ),
            },
        },
        "governance_scope": {
            "observation": "host_and_auto_gallery",
            "enforcement": "auto_gallery_only",
            "modifies_other_projects": False,
            "modifies_host_configuration": False,
        },
        "status": status,
    }


@router.put("/settings")
async def update_settings(data: AdminSettingsUpdate, db: AsyncSession = Depends(get_db)):
    sync_scan_reschedule = None
    if data.dedup is not None:
        await _put_setting(db, "dedup", data.dedup.model_dump())
    if data.subscription_defaults is not None:
        subscription_defaults = data.subscription_defaults.model_dump()
        await _put_setting(db, "subscription_defaults", subscription_defaults)
        try:
            sync_scan_reschedule = _reschedule_subscription_sync_scan(subscription_defaults)
        except QueueAdmissionError:
            raise
        except Exception:
            logger.warning("Failed to reschedule subscription sync scan after settings update", exc_info=True)
    if data.download_defaults is not None:
        await _put_setting(db, "download_defaults", data.download_defaults.model_dump())
    if data.proxy is not None:
        await _put_setting(db, "proxy", data.proxy.model_dump())
    return {"status": "ok", "message": "Settings saved to database", "sync_scan_reschedule": sync_scan_reschedule}


# ── Proxy Test ──

@router.post("/proxy/test")
async def test_proxy_connectivity(db: AsyncSession = Depends(get_db)):
    """Test connectivity through the configured proxy to key external sites."""
    import urllib.error
    import urllib.parse
    import urllib.request
    import ssl
    import time

    config = await _get_setting(db, "proxy", DEFAULT_PROXY)
    enabled = config.get("enabled", False)
    ssl_verify = config.get("ssl_verify", True)

    # Build SSL context — skip verification if user disabled it (MITM proxy)
    ssl_ctx = ssl.create_default_context()
    if not ssl_verify:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    # Build proxy opener
    opener = None
    if enabled:
        proxies = {}
        http_proxy = config.get("http_proxy", "")
        https_proxy = config.get("https_proxy", "")
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        if proxies:
            proxy_handler = urllib.request.ProxyHandler(proxies)
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ssl_ctx))

    if opener is None:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))

    # ── Proxy reachability check ──
    proxy_reachable = None
    proxy_reachable_error = ""
    if enabled:
        proxy_url = config.get("http_proxy") or config.get("https_proxy", "")
        if proxy_url:
            import socket
            parsed_proxy = urllib.parse.urlsplit(proxy_url)
            if parsed_proxy.hostname:
                host = parsed_proxy.hostname
                port = parsed_proxy.port or 7890
                try:
                    sock = socket.create_connection((host, port), timeout=5)
                    sock.close()
                    proxy_reachable = True
                except Exception:
                    logger.warning(
                        "Configured proxy endpoint is unreachable",
                        exc_info=True,
                    )
                    proxy_reachable = False
                    proxy_reachable_error = "Cannot connect to the configured proxy endpoint."

    direct_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))

    targets = [
        {"name": "Pixiv", "url": "https://www.pixiv.net"},
        {"name": "Danbooru", "url": "https://danbooru.donmai.us"},
        {"name": "Danbooru API", "url": "https://danbooru.donmai.us/artists.json?limit=1"},
        {"name": "Iwara", "url": "https://www.iwara.tv"},
        {"name": "Twitter/X", "url": "https://x.com"},
        {"name": "Pinterest", "url": "https://www.pinterest.com"},
        {"name": "LOFTER", "url": "https://www.lofter.com"},
        {"name": "GitHub", "url": "https://github.com"},
        {"name": "Google", "url": "https://www.google.com"},
    ]

    import concurrent.futures
    TEST_TIMEOUT = 3  # per-test timeout, all 9 run concurrently with 9 workers

    def _test_one(t):
        name, url = t["name"], t["url"]
        # Direct
        d_ok, d_ms, d_err = False, 0, ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "auto-gallery/0.1"})
            start = time.monotonic()
            with direct_opener.open(req, timeout=TEST_TIMEOUT) as resp:
                d_ok = resp.status < 500
            d_ms = int((time.monotonic() - start) * 1000)
        except urllib.error.HTTPError as e:
            d_err, d_ok = f"HTTP {e.code}", e.code < 500
        except urllib.error.URLError as exc:
            d_err = (
                "Connection timed out"
                if isinstance(exc.reason, TimeoutError)
                else f"Connection failed ({type(exc.reason).__name__})"
            )
        except Exception:
            logger.warning("Direct connectivity probe failed for %s", name, exc_info=True)
            d_err = "Connection test failed"

        # Proxy
        p_ok, p_ms, p_err = False, 0, ""
        if enabled:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "auto-gallery/0.1"})
                start = time.monotonic()
                with opener.open(req, timeout=TEST_TIMEOUT) as resp:
                    p_ok = resp.status < 500
                p_ms = int((time.monotonic() - start) * 1000)
            except urllib.error.HTTPError as e:
                p_err, p_ok = f"HTTP {e.code}", e.code < 500
            except urllib.error.URLError as exc:
                p_err = (
                    "Connection timed out"
                    if isinstance(exc.reason, TimeoutError)
                    else f"Connection failed ({type(exc.reason).__name__})"
                )
            except Exception:
                logger.warning("Proxy connectivity probe failed for %s", name, exc_info=True)
                p_err = "Connection test failed"

        return {"name": name, "url": url,
                "direct_ok": d_ok, "direct_ms": d_ms, "direct_error": d_err,
                "proxy_ok": p_ok if enabled else None, "proxy_ms": p_ms if enabled else None,
                "proxy_error": p_err if enabled else ""}

    import concurrent.futures
    logger.info("Proxy test starting: enabled=%s proxy=%s targets=%d", enabled,
                config.get("http_proxy", "not set"), len(targets))

    def _run_all():
        # list(executor.map(...)) blocks until all probes finish (~TEST_TIMEOUT)
        # — offloaded so the event loop isn't held for the duration.
        with concurrent.futures.ThreadPoolExecutor(max_workers=9) as executor:
            return list(executor.map(_test_one, targets))
    results = await asyncio.to_thread(_run_all)
    ok = sum(1 for r in results if r["proxy_ok"])
    fail = sum(1 for r in results if r["proxy_ok"] is False)
    logger.info("Proxy test complete: %d OK, %d FAIL, %d total (reachable=%s)",
                ok, fail, len(results), proxy_reachable)

    return {
        "proxy_enabled": enabled,
        "proxy_reachable": proxy_reachable,
        "proxy_reachable_error": proxy_reachable_error,
        "proxy_config": {
            "http": config.get("http_proxy", "not set"),
            "https": config.get("https_proxy", "not set"),
        },
        "results": results,
    }


# ── Data Management ──

@router.post("/reset-settings")
async def reset_settings(db: AsyncSession = Depends(get_db)):
    """Reset all system settings to defaults."""
    return await clear_entity_data("settings", db)


# ── Scheduler ──
