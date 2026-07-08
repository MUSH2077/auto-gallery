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
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequireAdmin
from app.database import async_session, get_db
from app.models.system_setting import SystemSetting
from app.services import admin_data
from app.services.admin_data import ENTITIES, clear_entity_data
from app.services.operations import get_operation_status, set_operation_status
from app.services.redis_client import get_redis

router = APIRouter(dependencies=[RequireAdmin])
_clear_files = admin_data._clear_files

# ── Settings schemas ──

class DedupSettings(BaseModel):
    source_level_enabled: bool = False
    cross_source_enabled: bool = False
    auto_merge: bool = False
    phash_threshold: int = 8

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


# ── Helpers ──

async def _get_setting(db: AsyncSession, key: str, default: dict = None) -> dict:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row and isinstance(row.value, dict):
        return {**(default or {}), **row.value}
    return default or {}


async def _put_setting(db: AsyncSession, key: str, value: dict):
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    await db.commit()

    # Invalidate caches when relevant settings change
    if key == "proxy":
        try:
            from app.services.proxy import clear_proxy_cache
            clear_proxy_cache()
        except Exception:
            pass


def _job_is_sync_scan(job) -> bool:
    return "sync_subscriptions" in (getattr(job, "func_name", "") or str(job))


def _reschedule_subscription_sync_scan(config: dict) -> dict:
    """Replace pending subscription sync scan jobs with one using current config."""
    import redis as redis_lib
    from rq import Queue
    from rq.registry import ScheduledJobRegistry
    from app.jobs.subscription_sync import sync_subscriptions

    interval = max(int(config.get("scheduler_scan_interval_minutes", 60)), 5)
    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="scheduled", connection=r)
    scheduled_registry = ScheduledJobRegistry(queue=q)

    removed = 0
    for job_id in list(scheduled_registry.get_job_ids()):
        job = q.fetch_job(job_id)
        if job and _job_is_sync_scan(job):
            scheduled_registry.remove(job_id, delete_job=True)
            removed += 1

    for job in list(q.get_jobs()):
        if _job_is_sync_scan(job):
            q.remove(job.id)
            removed += 1

    job = q.enqueue_in(timedelta(minutes=interval), sync_subscriptions)
    return {"removed": removed, "job_id": job.id, "interval_minutes": interval}


DEFAULT_DEDUP = {"source_level_enabled": False, "cross_source_enabled": False, "auto_merge": False, "phash_threshold": 8}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False}


# ── Settings ──




@router.post("/cleanup-metadata-jsons")
async def cleanup_metadata_jsons():
    """Remove all gallery-dl metadata JSON files from downloads directory."""
    from app.jobs.import_runner import cleanup_metadata_jsons
    removed = await cleanup_metadata_jsons(settings.download_root)
    return {"status": "ok", "removed": removed}


@router.get("/import-progress")
async def import_progress():
    """Get current import job progress summary."""
    from app.models.import_job import ImportJob as IJ
    async with async_session() as db:
        result = await db.execute(
            select(IJ).order_by(IJ.created_at.desc()).limit(20)
        )
        jobs = result.scalars().all()
        return {
            "running": sum(1 for j in jobs if j.status == "running"),
            "enqueued": sum(1 for j in jobs if j.status == "enqueued"),
            "complete": sum(1 for j in jobs if j.status == "complete"),
            "failed": sum(1 for j in jobs if j.status == "failed"),
            "recent": [{"id": str(j.id), "status": j.status, "error": (j.error_log or "")[:200]} for j in jobs[:5]],
        }



@router.post("/backup/schedule")
async def schedule_backup(data: dict):
    """Schedule recurring auto-backup. {enabled: bool, interval_hours: int}"""
    import redis as redis_lib
    from rq import Queue
    from datetime import timedelta

    enabled = str(data.get("enabled", "")).lower() in ("true", "1", "yes")
    interval = int(data.get("interval_hours", 24))
    r = redis_lib.from_url(settings.redis_url)
    q = Queue(name="scheduled", connection=r)

    # Remove existing backup jobs
    for jid in list(q.scheduled_job_registry.get_job_ids()):
        job = q.fetch_job(jid)
        if job and "auto_backup" in str(job.func_name):
            q.remove(jid)

    if enabled:
        q.enqueue_in(timedelta(hours=interval),
            "app.jobs.backup.run_auto_backup", interval=interval, job_timeout=3600)
        return {"status": "ok", "message": f"Auto-backup enabled, every {interval}h"}
    return {"status": "ok", "message": "Auto-backup disabled"}


_system_info_cache: dict | None = None
_system_info_cache_ts: float = 0.0
_SYSTEM_INFO_CACHE_TTL = 60.0
_system_info_lock = asyncio.Lock()


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
        """Blocking: recursive size walks over the whole library. Offloaded to a
        thread so these rglob("*") passes never run on the event loop."""
        def _safe_file_size(path: Path) -> int:
            try:
                return path.stat().st_size if path.is_file() else 0
            except Exception:
                return 0

        def _safe_dir_size(path: Path) -> int:
            total = 0
            try:
                if not path.exists():
                    return 0
                for f in path.rglob("*"):
                    if f.is_file():
                        total += _safe_file_size(f)
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

        original_media_bytes = 0
        try:
            for source_dir in dl_root.iterdir():
                if source_dir.is_dir() and source_dir.name != ".backups":
                    original_media_bytes += _safe_dir_size(source_dir)
        except Exception:
            original_media_bytes = _safe_dir_size(dl_root) - archive_bytes - backup_bytes

        library_index_bytes = _safe_dir_size(lib_root)

        sources: dict = {}
        try:
            for source_dir in dl_root.iterdir():
                if not source_dir.is_dir():
                    continue
                source_name = source_dir.name
                total_size = 0
                creator_count = 0
                work_count = 0
                for creator_dir in source_dir.iterdir():
                    if not creator_dir.is_dir():
                        continue
                    creator_count += 1
                    for work_dir in creator_dir.iterdir():
                        if not work_dir.is_dir():
                            continue
                        work_count += 1
                        for f in work_dir.rglob("*"):
                            if f.is_file():
                                try:
                                    total_size += f.stat().st_size
                                except Exception:
                                    pass
                sources[source_name] = {
                    "size_mb": round(total_size / (1024 ** 2), 1),
                    "creator_count": creator_count,
                    "work_count": work_count,
                }
        except Exception:
            pass

        return {
            "archive_bytes": archive_bytes,
            "backup_bytes": backup_bytes,
            "original_media_bytes": original_media_bytes,
            "library_index_bytes": library_index_bytes,
            "sources": sources,
        }

    _fs = await asyncio.to_thread(_compute_fs_sizes)
    archive_bytes = _fs["archive_bytes"]
    backup_bytes = _fs["backup_bytes"]
    original_media_bytes = _fs["original_media_bytes"]
    library_index_bytes = _fs["library_index_bytes"]
    sources = _fs["sources"]

    # ── Resolve creator display names from the database ──
    # The filesystem uses gallery-dl-derived directory names which may
    # not match any DB field. We cross-reference via work_sources:
    #   work_dir name (source_work_id) → work_sources.source_creator_id
    #   → source_creators.creator_id → creators.display_name
    creator_display: dict[tuple[str, str], str] = {}
    creator_id_map: dict[tuple[str, str], str] = {}  # (source, dir_name) → display_name
    try:
        from app.models.work_source import WorkSource
        from app.models.source_creator import SourceCreator
        from app.models.creator import Creator

        if dl_root.exists():
            # Collect one sample work_id per creator directory for lookup
            lookup_pairs: list[tuple[str, str, str]] = []  # (source, creator_dir, work_id)
            for source_dir in dl_root.iterdir():
                if not source_dir.is_dir():
                    continue
                src = source_dir.name
                for creator_dir in source_dir.iterdir():
                    if not creator_dir.is_dir():
                        continue
                    for work_dir in creator_dir.iterdir():
                        if work_dir.is_dir():
                            lookup_pairs.append((src, creator_dir.name, work_dir.name))
                            break  # first work is enough for lookup

            if lookup_pairs:
                work_ids = [p[2] for p in lookup_pairs]
                # Batch query work_sources
                ws_result = await db.execute(
                    select(
                        WorkSource.source,
                        WorkSource.source_work_id,
                        WorkSource.source_creator_id,
                    ).where(WorkSource.source_work_id.in_(work_ids))
                )
                # work_id → source_creator_id
                work_to_sc: dict[str, str] = {}
                for row in ws_result:
                    ws_src, ws_wid, ws_scid = row[0], row[1], row[2]
                    if ws_scid:
                        work_to_sc[ws_wid] = ws_scid

                # source_creator_id → creator display_name
                sc_ids = set(work_to_sc.values())
                if sc_ids:
                    sc_result = await db.execute(
                        select(
                            SourceCreator.source_creator_id,
                            Creator.display_name,
                            Creator.name,
                            Creator.id,
                        )
                        .join(Creator, Creator.id == SourceCreator.creator_id)
                        .where(SourceCreator.source_creator_id.in_(list(sc_ids)))
                    )
                    sc_to_display: dict[str, str] = {}
                    sc_to_creator_id: dict[str, str] = {}
                    for row in sc_result:
                        scid, cdisplay, cname, cid = row[0], row[1], row[2], str(row[3]) if row[3] else None
                        sc_to_display[scid] = cdisplay or cname or scid
                        if cid:
                            sc_to_creator_id[scid] = cid

                    for src, dir_name, work_id in lookup_pairs:
                        scid = work_to_sc.get(work_id)
                        if scid and scid in sc_to_display:
                            creator_display[(src, dir_name)] = sc_to_display[scid]
                            cid = sc_to_creator_id.get(scid)
                            if cid:
                                creator_id_map[(src, dir_name)] = cid
                        else:
                            creator_display[(src, dir_name)] = dir_name
    except Exception:
        pass

    # Per-creator top breakdown (by storage)
    def _compute_creator_sizes() -> list:
        """Blocking per-creator size walk — offloaded off the event loop so this
        second full-library rglob pass doesn't freeze concurrent requests."""
        rows: list[tuple[str, str, str, int, int]] = []  # (name, display, source, size, work_count)
        try:
            for source_dir in dl_root.iterdir():
                if not source_dir.is_dir():
                    continue
                src = source_dir.name
                for creator_dir in source_dir.iterdir():
                    if not creator_dir.is_dir():
                        continue
                    cname = creator_dir.name
                    csize = 0
                    wc = 0
                    for work_dir in creator_dir.iterdir():
                        if not work_dir.is_dir():
                            continue
                        wc += 1
                        for f in work_dir.rglob("*"):
                            if f.is_file():
                                try:
                                    csize += f.stat().st_size
                                except Exception:
                                    pass
                    display = creator_display.get((src, cname), cname)
                    rows.append((cname, display, src, csize, wc))
        except Exception:
            pass
        return rows

    creators = []
    try:
        creator_sizes = await asyncio.to_thread(_compute_creator_sizes)
        # Sort by size descending, top 20
        creator_sizes.sort(key=lambda x: x[3], reverse=True)
        for cname, display, src, sz, wc in creator_sizes[:20]:
            entry = {
                "name": cname,
                "display_name": display,
                "source": src,
                "size_mb": round(sz / (1024 ** 2), 1),
                "work_count": wc,
            }
            cid = creator_id_map.get((src, cname))
            if cid:
                entry["creator_id"] = cid
            creators.append(entry)
    except Exception:
        pass

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
        result = await db.execute(text(
            "SELECT a.id, a.file_path, a.file_name FROM assets a WHERE a.file_path IS NOT NULL"
        ))
        asset_rows = result.fetchall()

        def _check_dead() -> list:
            """One os.path.exists per asset — O(assets) stat calls, offloaded."""
            found = []
            for row in asset_rows:
                fpath = row[1]
                if fpath and not os.path.exists(fpath):
                    found.append({
                        "asset_id": str(row[0]),
                        "file_path": fpath,
                        "file_name": row[2],
                    })
            return found

        dead_links = await asyncio.to_thread(_check_dead)
        if dead_links:
            issues.append({
                "type": "dead_links",
                "severity": "error",
                "count": len(dead_links),
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
    import urllib.request
    import urllib.error
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
            import socket, re
            m = re.match(r'https?://([^:/]+):?(\d+)?', proxy_url)
            if m:
                host = m.group(1)
                port = int(m.group(2)) if m.group(2) else 7890
                try:
                    sock = socket.create_connection((host, port), timeout=5)
                    sock.close()
                    proxy_reachable = True
                except Exception as e:
                    proxy_reachable = False
                    proxy_reachable_error = f"Cannot connect to {host}:{port} — {e}"

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
        except urllib.error.URLError as e:
            d_err = str(e.reason)[:120] if e.reason else str(e)[:120]
        except Exception as e:
            d_err = str(e)[:120]

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
            except urllib.error.URLError as e:
                p_err = str(e.reason)[:120] if e.reason else str(e)[:120]
            except Exception as e:
                p_err = str(e)[:120]

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

@router.post("/clear/{entity}")
async def clear_entity(entity: str, db: AsyncSession = Depends(get_db)):
    """Clear a specific entity or 'all' for complete cleanup."""
    try:
        admin_data._clear_files = _clear_files
        return await clear_entity_data(entity, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{exc}. Valid: all, {', '.join(ENTITIES.keys())}",
        ) from exc


@router.post("/operations/clear")
async def start_clear_operation(data: dict, db: AsyncSession = Depends(get_db)):
    """Enqueue a data-management clear operation and return immediately."""
    from rq import Queue

    entity = str(data.get("entity") or "").strip()
    if entity != "all" and entity not in ENTITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity: {entity}. Valid: all, {', '.join(ENTITIES.keys())}",
        )

    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    task = await TaskService(db).create_task(
        task_id=UUID(job_id),
        kind="admin",
        operation_type="admin-clear",
        title=f"Clear {entity}",
        status="enqueued",
        queue_name="operations",
        meta={"entity": entity},
        progress={"phase": "enqueued", "label": f"Queued clear for {entity}"},
    )
    await db.commit()
    set_operation_status(
        job_id,
        "enqueued",
        "admin-clear",
        progress={"phase": "enqueued", "label": f"Queued clear for {entity}"},
        meta={"entity": entity},
    )
    queue = Queue(name="operations", connection=get_redis())
    rq_job = queue.enqueue(
        "app.jobs.admin_operations.run_clear_operation",
        entity,
        job_id,
        job_timeout=7200,
        result_ttl=7200,
    )
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()
    logger.info("Enqueued admin clear operation job_id=%s rq_job=%s entity=%s", job_id, rq_job.id, entity)
    return {"job_id": job_id, "status": "enqueued"}


@router.get("/operations/{job_id}")
async def get_admin_operation(job_id: str):
    status = get_operation_status(job_id)
    if not status:
        try:
            from app.services.tasks import TaskService, task_payload
            async with async_session() as db:
                task = await TaskService(db).get(UUID(job_id))
                if task and task.kind == "admin":
                    payload = task_payload(task)
                    return {
                        "job_id": payload["id"],
                        "status": payload["status"],
                        "operation_type": payload["operation_type"],
                        "progress": payload["progress_data"],
                        "result": payload["result_data"],
                        "error": payload["error_log"],
                        "meta": payload["meta"],
                        "updated_at": task.updated_at.timestamp() if task.updated_at else None,
                    }
        except ValueError:
            pass
        raise HTTPException(status_code=404, detail="Operation not found")
    return status


@router.get("/operations")
async def list_active_operations():
    """List all active (running + enqueued) admin operations."""
    import json
    from app.services.redis_client import get_redis
    r = get_redis()
    keys = r.keys("admin_operation:*")
    ops = []
    for k in keys:
        raw = r.get(k)
        if raw:
            try:
                payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                if payload.get("status") in ("queued", "enqueued", "running"):
                    ops.append(payload)
            except Exception:
                pass
    ops.sort(key=lambda o: o.get("updated_at", 0), reverse=True)
    return {"operations": ops}


@router.post("/reset-settings")
async def reset_settings(db: AsyncSession = Depends(get_db)):
    """Reset all system settings to defaults."""
    return await clear_entity_data("settings", db)


# ── Scheduler ──

class SchedulerSyncNowRequest(BaseModel):
    mode: Literal["force_eligible", "due_scan"] = "force_eligible"


@router.post("/scheduler/sync-now")
async def trigger_sync_now(data: SchedulerSyncNowRequest | None = None, db: AsyncSession = Depends(get_db)):
    """Manually trigger subscription sync work.

    ``force_eligible`` means "sync everything the operator would reasonably
    expect to sync now": active/enabled/downloadable sources with healthy auth,
    valid URLs, and no running job.  It bypasses schedule windows only.
    ``due_scan`` preserves the old behavior: run the scheduler's due-only scan.
    """
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.services.subscription_enqueue import enqueue_subscription_source_sync
    from app.services.tasks import TaskService

    mode = (data or SchedulerSyncNowRequest()).mode
    task_service = TaskService(db)
    parent_task = await task_service.create_task(
        kind="admin",
        operation_type="subscription-sync-batch",
        title="Sync eligible subscription sources" if mode == "force_eligible" else "Run scheduler sync scan",
        status="running",
        queue_name="downloads" if mode == "force_eligible" else "scheduled",
        progress={"phase": "scanning", "label": "Preparing subscription sync", "current": 0, "total": 0},
        meta={"mode": mode},
    )
    await db.commit()

    if mode == "due_scan":
        from app.jobs.subscription_sync import sync_subscriptions_async

        summary = await sync_subscriptions_async(parent_task_id=parent_task.id)
        result = {
            "mode": mode,
            "enqueued_count": int(summary.get("created", 0) or 0),
            "skipped_count": int(summary.get("skipped", 0) or 0),
            "error_count": int(summary.get("errors", 0) or 0),
            "skipped_reasons": {summary["reason"]: 1} if summary.get("reason") else {},
            "job_ids": [],
            "rescheduled_at": summary.get("rescheduled_at"),
        }
        await task_service.update_task(
            parent_task,
            status="complete" if summary.get("status") != "partial_error" else "failed",
            result=result,
            progress={"phase": "complete", "label": "Scheduler scan complete", "current": 1, "total": 1},
            error=summary.get("reason") if summary.get("status") == "skipped" else None,
        )
        await db.commit()
        return {"status": summary.get("status", "ok"), "task_id": str(parent_task.id), "message": "Scheduler scan complete", **result}

    rows = list((await db.execute(
        select(SubscriptionSource)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .where(
            and_(
                Subscription.is_active == True,  # noqa: E712
                Subscription.sync_enabled == True,  # noqa: E712
                SubscriptionSource.is_enabled == True,  # noqa: E712
            )
        )
        .order_by(SubscriptionSource.last_synced_at.asc().nullsfirst(), SubscriptionSource.created_at.asc())
    )).scalars().all())

    await task_service.update_task(
        parent_task,
        progress={"phase": "enqueuing", "label": "Enqueuing eligible sources", "current": 0, "total": len(rows)},
    )
    await db.commit()

    job_ids: list[str] = []
    task_ids: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    skipped_reasons: dict[str, int] = {}

    for index, ss in enumerate(rows, start=1):
        result = await enqueue_subscription_source_sync(
            db,
            ss.id,
            trigger="manual_scheduler_batch",
            force=False,
            parent_task_id=parent_task.id,
            force_reason="scheduler_force_eligible",
        )
        if result.get("status") == "enqueued":
            job_ids.append(result["job_id"])
            if result.get("task_id"):
                task_ids.append(result["task_id"])
        elif result.get("status") == "error":
            errors.append(result)
            code = result.get("reason", {}).get("code") or result.get("skip_reason") or "error"
            skipped_reasons[code] = skipped_reasons.get(code, 0) + 1
        else:
            skipped.append(result)
            code = result.get("skip_reason") or result.get("reason", {}).get("code") or "skipped"
            skipped_reasons[code] = skipped_reasons.get(code, 0) + 1
        await task_service.update_task(
            parent_task,
            progress={"phase": "enqueuing", "label": "Enqueuing eligible sources", "current": index, "total": len(rows)},
        )
        await db.commit()

    response = {
        "mode": mode,
        "enqueued_count": len(job_ids),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "skipped_reasons": skipped_reasons,
        "job_ids": job_ids,
        "task_ids": task_ids,
        "skipped": skipped,
        "errors": errors,
    }
    status = "partial_error" if errors else "ok"
    await task_service.update_task(
        parent_task,
        status="complete" if not errors else "failed",
        result=response,
        progress={"phase": "complete", "label": "Subscription sync batch complete", "current": len(rows), "total": len(rows)},
        error=f"{len(errors)} enqueue attempts failed" if errors else None,
    )
    await db.commit()
    return {"status": status, "task_id": str(parent_task.id), "message": f"Enqueued {len(job_ids)} download jobs, skipped {len(skipped)} sources", **response}


# ── Search ──

@router.post("/search/reindex")
async def reindex_search(db: AsyncSession = Depends(get_db)):
    from app.services.search import SearchService
    svc = SearchService(db)
    return await svc.reindex()


# ── Library ──

class RebuildLibraryRequest(BaseModel):
    mode: Literal["repair", "full"] = "repair"
    source: str | None = None
    creator_id: UUID | None = None
    work_id: UUID | None = None
    resume: bool = True


@router.post("/library/rebuild")
async def rebuild_library(data: RebuildLibraryRequest | None = None):
    """Enqueue a library rebuild operation and return immediately."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status

    options = (data or RebuildLibraryRequest()).model_dump(mode="json")
    redis = get_redis()
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    active_job = redis.get("library:rebuild:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:rebuild:active")
    if not redis.set("library:rebuild:active", job_id, nx=True, ex=604800):
        active_job = redis.get("library:rebuild:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Library rebuild already running", "job_id": active_job})
    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-rebuild",
            title="Rebuild library index",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Library rebuild queued"},
            meta={"entity": "library", **options},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-rebuild",
        progress={"phase": "enqueued", "label": "Library rebuild queued"},
        meta={"entity": "library", **options})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_library_rebuild_operation",
        job_id, options, job_timeout=14400, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Library rebuild queued", "options": options}


class ImportFromDiskRequest(BaseModel):
    source: str | None = None
    # Reprocess every on-disk file even if the ledger marks it 'done'. Use this
    # to recover creators/works after they were deleted from the DB (the ledger
    # is not reset by deletion, so a normal disk-import skips them as 'done').
    reset_ledger: bool = False


@router.post("/library/import-from-disk")
async def import_from_disk(data: ImportFromDiskRequest | None = None):
    """Enqueue an idempotent import of on-disk download files into the DB."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status

    options = (data or ImportFromDiskRequest()).model_dump(mode="json")
    redis = get_redis()
    job_id = str(uuid.uuid4())
    from app.services.tasks import TaskService
    active_job = redis.get("library:disk-import:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:disk-import:active")
    if not redis.set("library:disk-import:active", job_id, nx=True, ex=604800):
        active_job = redis.get("library:disk-import:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Disk import already running", "job_id": active_job})
    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-disk-import",
            title="Import from disk",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Disk import queued"},
            meta={"entity": "disk-import", **options},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-disk-import",
        progress={"phase": "enqueued", "label": "Disk import queued"},
        meta={"entity": "disk-import", **options})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_disk_import_operation",
        job_id, options, job_timeout=14400, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Disk import queued", "options": options}


@router.post("/creators/re-enrich")
async def reenrich_creators():
    """Enqueue a Danbooru re-enrichment sweep for creators flagged needs_enrichment."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status
    from app.services.tasks import TaskService

    redis = get_redis()
    job_id = str(uuid.uuid4())
    active_job = redis.get("library:creator-reenrich:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:creator-reenrich:active")
    if not redis.set("library:creator-reenrich:active", job_id, nx=True, ex=86400):
        active_job = redis.get("library:creator-reenrich:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={
            "message": "Creator re-enrichment already running", "job_id": active_job})
    async with async_session() as task_db:
        task = await TaskService(task_db).create_task(
            task_id=UUID(job_id),
            kind="admin",
            operation_type="admin-creator-reenrich",
            title="Re-enrich creators from Danbooru",
            status="enqueued",
            queue_name="operations",
            progress={"phase": "enqueued", "label": "Creator re-enrichment queued"},
            meta={"entity": "creator-reenrich"},
        )
        await task_db.commit()
    set_operation_status(job_id, "enqueued", "admin-creator-reenrich",
        progress={"phase": "enqueued", "label": "Creator re-enrichment queued"},
        meta={"entity": "creator-reenrich"})

    rq_job = Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_creator_reenrich_operation",
        job_id, {}, job_timeout=7200, result_ttl=604800)
    async with async_session() as task_db:
        svc = TaskService(task_db)
        current = await svc.get(task.id)
        if current:
            await svc.update_task(current, rq_job_id=rq_job.id)
            await task_db.commit()

    return {"status": "enqueued", "job_id": job_id, "message": "Creator re-enrichment queued"}


# ── gallery-dl Config ──

class PixivSourceConfig(BaseModel):
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    refresh_token: str | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None
    include: str | None = "artworks"
    tags: str | None = "japanese"
    ugoira: str | None = "zip"
    sleep_request: float | None = None
    max_posts: int | None = None
    metadata: bool | None = None
    metadata_bookmark: bool | None = None
    captions: bool | None = None
    comments: bool | None = None
    sanity: bool | None = None

class TwitterSourceConfig(BaseModel):
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None
    strategy: str | None = "tweets"
    include: str | None = "timeline"
    retweets: bool | None = False
    replies: bool | None = False
    cards: bool | None = True
    videos: bool | None = True
    text_tweets: bool | None = False
    quoted: bool | None = False
    pinned: bool | None = None
    previews: bool | None = None
    articles: bool | None = None
    max_posts: int | None = None

class IwaraSourceConfig(BaseModel):
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    username: str | None = None
    password: str | None = None
    filename: str | None = None
    directory: str | None = None
    format: str | None = None
    include: str | None = None

class DanbooruSourceConfig(BaseModel):
    model_config = {"extra": "ignore"}
    auto_enable_on_import: bool | None = None
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    external: bool | None = None
    metadata: bool | None = None
    filename: str | None = None
    directory: str | None = None


class PinterestSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    domain: str | None = None
    stories: bool | None = True
    videos: bool | None = True
    sections: bool | None = True
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None


class LofterSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    filename: str | None = None
    directory: str | None = None


class WeiboSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    cookies_path: str | None = None
    cookie_content: str | None = None
    videos: bool | None = None
    retweets: bool | None = None
    gifs: bool | None = None
    livephoto: bool | None = None
    movies: bool | None = None
    text: bool | None = None
    include: str | None = None
    filename: str | None = None
    directory: str | None = None


class BilibiliSourceConfig(BaseModel):
    auto_enable_on_import: bool | None = None
    livephoto: bool | None = None
    sleep_request: str | None = None
    filename: str | None = None
    directory: str | None = None


class GalleryDLMultiConfig(BaseModel):
    pixiv: PixivSourceConfig | None = None
    twitter: TwitterSourceConfig | None = None
    iwara: IwaraSourceConfig | None = None
    danbooru: DanbooruSourceConfig | None = None
    pinterest: PinterestSourceConfig | None = None
    lofter: LofterSourceConfig | None = None
    weibo: WeiboSourceConfig | None = None
    bilibili: BilibiliSourceConfig | None = None

GALLERYDL_SOURCE_OPTIONS = {
    "pixiv": {
        "name": "Pixiv",
        "supported": True,
        "description": "Pixiv illustrations, manga, and ugoira.",
    },
    "twitter": {
        "name": "X / Twitter",
        "supported": True,
        "description": "Twitter/X timeline, media, and tweets.",
    },
    "iwara": {
        "name": "Iwara",
        "supported": True,
        "description": "Iwara.tv videos, images, playlists, and user profiles. Requires gallery-dl >= 1.32.",
    },
    "danbooru": {
        "name": "Danbooru",
        "supported": True,
        "description": "Danbooru post download and reference — artist tracking, tag monitoring, metadata enrichment.",
    },
    "pinterest": {
        "name": "Pinterest",
        "supported": True,
        "description": "Pinterest pins, boards, and user profiles. No authentication required — public API only.",
    },
    "lofter": {
        "name": "LOFTER",
        "supported": True,
        "description": "LOFTER blog posts and images. Chinese platform. No authentication required.",
    },
    "weibo": {
        "name": "微博 (Weibo)",
        "supported": True,
        "description": "Weibo user feeds, albums, and statuses. Chinese platform. Cookies optional.",
    },
    "bilibili": {
        "name": "哔哩哔哩 (Bilibili)",
        "supported": True,
        "description": "Bilibili articles and user galleries. No authentication required for public content.",
    },
}

PIXIV_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "refresh_token": "refresh-token", "cookies_path": "cookies",
    "filename": "filename", "directory": "directory",
    "include": "include", "tags": "tags", "ugoira": "ugoira",
    "sleep_request": "sleep-request", "max_posts": "max-posts",
    "metadata": "metadata", "metadata_bookmark": "metadata-bookmark",
    "captions": "captions", "comments": "comments", "sanity": "sanity",
}

TWITTER_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory", "strategy": "strategy", "include": "include",
    "retweets": "retweets", "replies": "replies",
    "cards": "cards", "videos": "videos",
    "text_tweets": "text-tweets", "quoted": "quoted",
    "pinned": "pinned", "previews": "previews", "articles": "articles",
    "max_posts": "max-posts",
}

IWARA_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "username": "username",
    "password": "password", "filename": "filename",
    "directory": "directory", "format": "format",
    "include": "include",
}

DANBOORU_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "username": "username", "password": "password",
    "api_key": "api-key", "cookies_path": "cookies",
        "external": "external", "metadata": "metadata",
    "filename": "filename", "directory": "directory",
}


PINTEREST_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "domain": "domain", "stories": "stories",
    "videos": "videos", "sections": "sections",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory",
}

LOFTER_CONFIG_MAP = {
    "cookie_content": "cookies-content",
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies", "filename": "filename",
    "directory": "directory",
}

WEIBO_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "cookies_path": "cookies",
    "videos": "videos",
    "retweets": "retweets",
    "gifs": "gifs",
    "livephoto": "livephoto",
    "movies": "movies",
    "text": "text",
    "include": "include",
    "filename": "filename",
    "directory": "directory",
}

BILIBILI_CONFIG_MAP = {
    "auto_enable_on_import": "auto-enable-on-import",
    "livephoto": "livephoto",
    "sleep_request": "sleep-request",
    "filename": "filename",
    "directory": "directory",
}

def _convert_to_netscape_cookies(raw: str, source: str) -> str:
    """Convert browser-pasted cookies to Netscape format that gallery-dl expects.

    Accepts:
    - Header format: ``name1=value1; name2=value2`` (one line)
    - Line format: ``name=value`` (one per line)
    - Netscape format: already 7 tab-separated fields (returned as-is)

    Returns Netscape-formatted cookie content.
    """
    # If already Netscape format (tab-separated, 7 fields), return as-is
    lines = [l.strip() for l in raw.split("\n") if l.strip() and not l.startswith("#")]
    if lines:
        first = lines[0]
        if "\t" in first and len(first.split("\t")) >= 6:
            return raw
        # Check if it's header-style (contains "; " semicolon separators)
        if "; " in raw or ";" in raw:
            # Header format: split by ";" into individual cookies
            cookies = []
            for part in raw.replace("; ", ";").split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, value = part.partition("=")
                    cookies.append((name.strip(), value.strip()))
        else:
            # One cookie per line: name=value
            cookies = []
            for line in lines:
                if "=" in line:
                    name, _, value = line.partition("=")
                    cookies.append((name.strip(), value.strip()))
    else:
        return raw

    if not cookies:
        return raw

    # Map source to appropriate domain
    domain_map = {
        "twitter": ".twitter.com",
        "x": ".x.com",
        "pixiv": ".pixiv.net",
        "danbooru": ".danbooru.donmai.us",
        "iwara": ".iwara.tv",
        "pinterest": ".pinterest.com",
        "lofter": ".lofter.com",
        "weibo": ".weibo.com",
        "bilibili": ".bilibili.com",
    }
    domain = domain_map.get(source, f".{source}.com")
    far_future = "9999999999"

    out = ["# Netscape HTTP Cookie File"]
    for name, value in cookies:
        out.append(f"{domain}\tTRUE\t/\tFALSE\t{far_future}\t{name}\t{value}")

    logger.info("Converted %d cookies to Netscape format for %s", len(cookies), source)
    return "\n".join(out)


def _load_config() -> tuple[dict, str]:
    from app.services.settings import ensure_gallerydl_config, gallerydl_config_path

    config_path = gallerydl_config_path()
    return ensure_gallerydl_config(config_path), str(config_path)

def _read_source_config(extractor_config: dict, schema_map: dict, config: dict | None = None) -> dict:
    """Read gallery-dl extractor config back into our API schema shape."""
    result = {}
    for api_key, dl_key in schema_map.items():
        val = extractor_config.get(dl_key)
        if api_key == "directory" and isinstance(val, list):
            val = "/".join(val)
        if api_key == "ugoira" and isinstance(val, bool):
            val = "zip"
        if val is not None:
            result[api_key] = val
    return result

def _write_source_config(extractor_config: dict, data: BaseModel, schema_map: dict, dir_keys: set = None) -> dict:
    """Merge API schema fields back into gallery-dl extractor config."""
    if dir_keys is None:
        dir_keys = {"directory"}
    for api_key, dl_key in schema_map.items():
        val = getattr(data, api_key, None)
        if val is not None:
            if dl_key in dir_keys and isinstance(val, str) and "/" in val:
                extractor_config[dl_key] = val.split("/")
            elif isinstance(val, str) and val == "":
                # Remove empty strings — they break gallery-dl defaults
                extractor_config.pop(dl_key, None)
            else:
                extractor_config[dl_key] = val
    return extractor_config


def _rebuild_managed_postprocessors(config: dict, pixiv_ugoira: str | None) -> list[dict]:
    """Rebuild auto-gallery managed gallery-dl postprocessors.

    The metadata postprocessor is required by the import pipeline. The ugoira
    postprocessor is controlled by the current Pixiv config and must survive
    saves from other provider tabs.

    When ugoira format conversion is enabled (gif/webm/mp4), this mirrors the
    behavior of gallery-dl's --ugoira CLI flag:
    1. Sets extractor.pixiv.ugoira to "original" so the extractor downloads
       individual frames instead of a ZIP archive
    2. Adds a ugoira postprocessor to assemble frames into the chosen format
    """
    postprocessors = config.setdefault("postprocessors", [])
    postprocessors[:] = [
        pp for pp in postprocessors
        if not (isinstance(pp, dict) and pp.get("name") in ("ugoira", "metadata"))
    ]

    if pixiv_ugoira in ("gif", "webm", "mp4"):
        postprocessors.append({
            "name": "ugoira",
            "extension": pixiv_ugoira,
            "keep-files": False,
        })

    postprocessors.append({
        "name": "metadata",
        "event": "after",
        "filename": "{filename}.json",
    })
    return postprocessors


# ── Gallery-dl connectivity test ──

TEST_URLS = {
    "pixiv": "https://www.pixiv.net/users/11",
    "twitter": "https://x.com/X",
    "iwara": "https://www.iwara.tv/videos",
    "danbooru": "https://danbooru.donmai.us/posts?tags=rating:s&limit=1",
    "pinterest": "https://www.pinterest.com/pinterest/",
    "lofter": "https://www.lofter.com/tag/%E6%8F%92%E7%94%BB",
    "weibo": "https://weibo.com/u/2803301701",
    "bilibili": "https://space.bilibili.com/8047632",
}

AUTH_ERROR_PATTERNS = [
    (r"(?i)AuthRequired", "Authentication failed — invalid or missing credentials"),
    (r"(?i)AuthorizationError", "Authorization denied — check account permissions"),
    (r"(?i)HttpError.*401", "HTTP 401 Unauthorized — check username/password"),
    (r"(?i)HttpError.*403", "HTTP 403 Forbidden — check account access"),
    (r"(?i)LoginRequired", "Login required — credentials not provided or invalid"),
    (r"(?i)NotFoundError", "User/page not found — check the test URL"),
    (r"(?i)cookie.*(?:expired|invalid|missing)", "Cookie expired or invalid"),
    (r"(?i)token.*(?:expired|invalid|revoked)", "Token expired or revoked"),
    (r"(?i)Could not authenticate", "Authentication rejected by server"),
]

@router.post("/gallerydl-config/test-connection")
async def test_source_connection(data: dict):
    """Test gallery-dl connectivity for a given source using current credentials."""
    source = data.get("source", "")
    if source not in TEST_URLS:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    test_url = TEST_URLS[source]
    import json as _json, os, re as _re, subprocess, tempfile, shutil

    tmpdir = tempfile.mkdtemp(prefix="gallerydl-test-")
    tmp_config = os.path.join(tmpdir, "test-config.json")
    try:
        from app.providers import registry as _registry
        from app.services.settings import (
            build_effective_gallerydl_config,
            source_key_for_extractor,
        )

        provider_source = source_key_for_extractor(source)
        provider_config = {}
        try:
            provider = _registry.get(provider_source)
            provider_config = provider.build_gallerydl_config(None)
        except KeyError:
            provider_config = {}
        config = build_effective_gallerydl_config(provider_source, provider_config)
        with open(tmp_config, "w") as f:
            _json.dump(config, f)

        cmd = [
            "gallery-dl", "--config", tmp_config,
            "--write-metadata",
            "--range", "1-1",
            "--destination", tmpdir,
            test_url,
        ]

        env = os.environ.copy()
        proxy_enabled = False
        try:
            from app.services.proxy import _load_proxy_config, get_proxy_env
            proxy_config = await _load_proxy_config()
            proxy_enabled = bool(proxy_config.get("enabled", False))
            env.update(get_proxy_env(proxy_config))
        except Exception:
            logger.warning("Failed to apply proxy env for gallery-dl test", exc_info=True)

        logger.info("Testing %s connectivity: %s (proxy=%s)", source, " ".join(cmd), proxy_enabled)
        # gallery-dl can run up to 120s — off the event loop so a connection
        # test doesn't freeze the whole backend for other users.
        result = await asyncio.to_thread(
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env))
        stderr = result.stderr or ""
        stdout = result.stdout or ""
        combined = stdout + stderr

        auth_issue = None
        for pattern, label in AUTH_ERROR_PATTERNS:
            if _re.search(pattern, combined):
                auth_issue = label
                break

        if result.returncode == 0 and not auth_issue:
            return {
                "source": source, "success": True,
                "message": f"Connection test passed for {source}. Credentials are valid.",
                "details": stderr[:1000] or "(no output)",
            }
        elif auth_issue:
            return {
                "source": source, "success": False,
                "message": auth_issue,
                "details": (stderr or stdout or "")[:2000],
            }
        else:
            # Show last portion of stderr (gallery-dl puts the actual error at the end)
            detail = (stderr or stdout or "").strip()
            if len(detail) > 2000:
                detail = "...(truncated)\n" + detail[-2000:]
            return {
                "source": source, "success": False,
                "message": f"gallery-dl exited with code {result.returncode}",
                "details": detail or "(no output — check gallery-dl version and network connectivity)",
            }

    except subprocess.TimeoutExpired:
        return {
            "source": source, "success": False,
            "message": "Connection test timed out after 120s",
            "details": "The request to the remote server took too long.",
        }
    except Exception as e:
        logger.error("Connection test failed for %s: %s", source, e)
        return {
            "source": source, "success": False,
            "message": f"Test failed: {str(e)[:200]}",
            "details": str(e),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@router.get("/gallerydl-config/effective")
async def get_effective_gallerydl_config(
    source: str,
    subscription_source_id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Preview the effective per-job gallery-dl config used by workers."""
    from uuid import UUID

    from app.models.subscription_source import SubscriptionSource
    from app.providers import registry as _registry
    from app.services.job_manifest import redacted_manifest_config
    from app.services.settings import (
        build_effective_gallerydl_config,
        extractor_key_for_source,
        source_key_for_extractor,
    )

    provider_source = source_key_for_extractor(source)
    source_url = None
    if subscription_source_id:
        ss = await db.get(SubscriptionSource, UUID(subscription_source_id))
        if not ss:
            raise HTTPException(status_code=404, detail="Subscription source not found")
        provider_source = ss.source
        source_url = ss.source_url

    try:
        provider = _registry.get(provider_source)
    except KeyError:
        raise HTTPException(status_code=400, detail={
            "code": "unknown_provider",
            "message": f"Unknown source provider: {provider_source}",
            "retryable": False,
        })

    provider_config = provider.build_gallerydl_config(None)
    effective = build_effective_gallerydl_config(provider_source, provider_config)

    url_valid = bool(source_url and provider.validate_url(provider.normalize_url(source_url) or source_url)) if source_url else None
    return {
        "source": provider_source,
        "extractor": extractor_key_for_source(provider_source),
        "source_url": source_url,
        "url_valid": url_valid,
        "config": redacted_manifest_config(effective),
    }


@router.get("/gallerydl-config")
async def get_gallerydl_config(source: str | None = None):
    """Get gallery-dl config for one or all sources. Pass ?source=pixiv|twitter|iwara."""
    config, _ = _load_config()
    extractors = config.get("extractor", {})

    def get_source(source_name, schema_map):
        src = extractors.get(source_name, {})
        return _read_source_config(src, schema_map, config)

    all_config = {
        "pixiv": get_source("pixiv", PIXIV_CONFIG_MAP),
        "twitter": get_source("twitter", TWITTER_CONFIG_MAP),
        "iwara": get_source("iwara", IWARA_CONFIG_MAP),
        "danbooru": get_source("danbooru", DANBOORU_CONFIG_MAP),
        "pinterest": get_source("pinterest", PINTEREST_CONFIG_MAP),
        "lofter": get_source("lofter", LOFTER_CONFIG_MAP),
        "weibo": get_source("weibo", WEIBO_CONFIG_MAP),
        "bilibili": get_source("bilibili", BILIBILI_CONFIG_MAP),
        "sources": GALLERYDL_SOURCE_OPTIONS,
    }
    # Read cookie file contents for sources that have cookies configured
    for src_name, src_cfg in all_config.items():
        if isinstance(src_cfg, dict) and src_cfg.get("cookies_path"):
            cookie_path = os.path.join(
                os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"),
                src_cfg["cookies_path"].lstrip("/"))
            if os.path.exists(cookie_path):
                try:
                    with open(cookie_path) as cf:
                        src_cfg["cookie_content"] = cf.read().strip()
                except Exception:
                    pass

    if source:
        return {source: all_config.get(source, {}), "sources": GALLERYDL_SOURCE_OPTIONS}
    return all_config


@router.put("/gallerydl-config")
async def update_gallerydl_config(data: GalleryDLMultiConfig):
    """Update gallery-dl extractor configs. Only provided source blocks are updated."""
    import os
    from app.services.settings import write_gallerydl_config

    config, config_path = _load_config()

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    extractors = config.setdefault("extractor", {})

    # Write cookie content to files (auto-sets cookies_path if not provided)
    cookies_dir = os.path.join(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "cookies")
    for source_name, source_data, source_map in [
        ("pixiv", data.pixiv, PIXIV_CONFIG_MAP),
        ("twitter", data.twitter, TWITTER_CONFIG_MAP),
        ("iwara", data.iwara, IWARA_CONFIG_MAP),
        ("danbooru", data.danbooru, DANBOORU_CONFIG_MAP),
        ("pinterest", data.pinterest, PINTEREST_CONFIG_MAP),
        ("lofter", data.lofter, LOFTER_CONFIG_MAP),
        ("weibo", data.weibo, WEIBO_CONFIG_MAP),
        ("bilibili", data.bilibili, BILIBILI_CONFIG_MAP),
    ]:
        if source_data is None:
            continue
        cc = getattr(source_data, "cookie_content", None)
        if cc and cc.strip():
            content = _convert_to_netscape_cookies(cc.strip(), source_name)
            os.makedirs(cookies_dir, exist_ok=True)
            cookie_path = os.path.join(cookies_dir, f"{source_name}.txt")
            with open(cookie_path, "w") as f:
                f.write(content + "\n")
            # Auto-set cookies_path if not explicitly provided
            if not getattr(source_data, "cookies_path", None):
                source_data.cookies_path = f"/gallerydl-config/cookies/{source_name}.txt"

    if data.pixiv is not None:
        pixiv = extractors.setdefault("pixiv", {})
        _write_source_config(pixiv, data.pixiv, PIXIV_CONFIG_MAP)
        extractors["pixiv"] = pixiv

    if data.twitter is not None:
        twitter = extractors.setdefault("twitter", {})
        _write_source_config(twitter, data.twitter, TWITTER_CONFIG_MAP)
        extractors["twitter"] = twitter

    if data.iwara is not None:
        iwara = extractors.setdefault("iwara", {})
        _write_source_config(iwara, data.iwara, IWARA_CONFIG_MAP)
        extractors["iwara"] = iwara

    if data.danbooru is not None:
        danbooru = extractors.setdefault("danbooru", {})
        _write_source_config(danbooru, data.danbooru, DANBOORU_CONFIG_MAP)
        extractors["danbooru"] = danbooru

    if data.pinterest is not None:
        pinterest = extractors.setdefault("pinterest", {})
        _write_source_config(pinterest, data.pinterest, PINTEREST_CONFIG_MAP)
        extractors["pinterest"] = pinterest

    if data.lofter is not None:
        lofter = extractors.setdefault("lofter", {})
        _write_source_config(lofter, data.lofter, LOFTER_CONFIG_MAP)
        extractors["lofter"] = lofter

    if data.weibo is not None:
        weibo = extractors.setdefault("weibo", {})
        _write_source_config(weibo, data.weibo, WEIBO_CONFIG_MAP)
        extractors["weibo"] = weibo

    if data.bilibili is not None:
        bilibili = extractors.setdefault("bilibili", {})
        _write_source_config(bilibili, data.bilibili, BILIBILI_CONFIG_MAP)
        extractors["bilibili"] = bilibili

    config["extractor"] = extractors

    # Ensure managed postprocessors match the final saved config. This keeps
    # Pixiv GIF conversion stable even when saving a non-Pixiv provider tab.
    pixiv_ugoira = extractors.get("pixiv", {}).get("ugoira")
    _rebuild_managed_postprocessors(config, pixiv_ugoira)

    write_gallerydl_config(config, Path(config_path))
    return {"status": "ok", "message": "gallery-dl config updated", "path": config_path}


# ── Dedup ──

@router.get("/dedup/duplicates")
async def list_duplicates():
    from app.database import async_session
    from app.models import WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(
                WorkSource.source,
                WorkSource.source_work_id,
                func.count(WorkSource.id).label("cnt"),
                func.array_agg(WorkSource.work_id).label("work_ids"),
            )
            .group_by(WorkSource.source, WorkSource.source_work_id)
            .having(func.count(WorkSource.id) > 1)
            .limit(100)
        )
        duplicates = []
        for row in result:
            duplicates.append({
                "source": row.source,
                "source_work_id": row.source_work_id,
                "count": row.cnt,
                "work_ids": [str(w) for w in row.work_ids],
            })
        return {"duplicates": duplicates, "total": len(duplicates)}


@router.post("/dedup/scan")
async def scan_duplicates():
    from app.database import async_session
    from app.models import WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(func.count(func.distinct(WorkSource.source + WorkSource.source_work_id)).label("unique_works"),
                   func.count(WorkSource.id).label("total_sources"))
        )
        row = result.one()
        return {
            "status": "ok",
            "unique_works": row.unique_works,
            "total_source_records": row.total_sources,
            "message": "Dedup scan completed.",
        }


# ── Merge Candidates ──

@router.get("/merge-candidates")
async def list_merge_candidates():
    from app.database import async_session
    from app.models import Work, WorkSource
    from sqlalchemy import func, select

    async with async_session() as db:
        result = await db.execute(
            select(
                Work.title,
                func.count(func.distinct(WorkSource.source)).label("source_count"),
                func.array_agg(func.distinct(WorkSource.source)).label("sources"),
                func.array_agg(Work.id).label("work_ids"),
            )
            .join(WorkSource, WorkSource.work_id == Work.id)
            .where(Work.title.isnot(None))
            .group_by(Work.title)
            .having(func.count(func.distinct(WorkSource.source)) > 1)
            .limit(100)
        )
        candidates = []
        for row in result:
            candidates.append({
                "title": row.title,
                "source_count": row.source_count,
                "sources": [str(s) for s in row.sources],
                "work_ids": [str(w) for w in row.work_ids],
            })
        return {"candidates": candidates, "total": len(candidates)}


@router.post("/merge-candidates/{work_id}/merge")
async def merge_works(work_id: str, data: dict):
    return {"status": "not_implemented", "message": "Merge functionality is deferred to a future phase."}


# ── Auth Status ──

@router.get("/auth-status")
async def get_auth_status(db: AsyncSession = Depends(get_db)):
    """List auth health for all subscription sources."""
    from app.models import SubscriptionSource, Subscription, Creator
    from sqlalchemy import select as s

    result = await db.execute(
        s(SubscriptionSource, Subscription, Creator)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .join(Creator, Creator.id == Subscription.creator_id)
        .order_by(SubscriptionSource.auth_healthy.asc())
    )
    sources = []
    for ss, sub, creator in result:
        sources.append({
            "id": str(ss.id),
            "source": ss.source,
            "source_url": ss.source_url,
            "source_creator_id": ss.source_creator_id,
            "auth_healthy": ss.auth_healthy,
            "auth_status": ss.auth_status,
            "auth_error_reason": ss.auth_error_reason,
            "last_auth_checked_at": ss.last_auth_checked_at.isoformat() if ss.last_auth_checked_at else None,
            "last_successful_auth": ss.last_successful_auth.isoformat() if ss.last_successful_auth else None,
            "is_enabled": ss.is_enabled,
            "subscription": {
                "id": str(sub.id),
                "name": sub.name,
                "is_active": sub.is_active,
                "sync_enabled": sub.sync_enabled,
            },
            "creator": {
                "id": str(creator.id),
                "name": creator.name,
                "display_name": creator.display_name,
            },
        })
    healthy = sum(1 for s in sources if s["auth_healthy"] is True)
    unhealthy = sum(1 for s in sources if s["auth_healthy"] is False)
    unknown = sum(1 for s in sources if s["auth_healthy"] is None)
    return {
        "sources": sources,
        "summary": {"total": len(sources), "healthy": healthy, "unhealthy": unhealthy, "unknown": unknown},
    }


@router.post("/auth-status/{source_id}/reset")
async def reset_auth_health(source_id: UUID, db: AsyncSession = Depends(get_db)):
    """Reset auth health for a subscription source — allows sync to resume.

    Use this after fixing credentials (e.g. updating refresh token, cookies,
    or password). The next download attempt will re-evaluate auth health.
    """
    from app.models import SubscriptionSource

    ss = await db.get(SubscriptionSource, source_id)
    if not ss:
        raise HTTPException(status_code=404, detail="Subscription source not found")

    ss.auth_healthy = True
    ss.auth_status = None
    ss.auth_error_reason = None
    ss.last_auth_checked_at = None
    await db.commit()

    return {
        "status": "ok",
        "message": f"Auth health reset for source {source_id}. Next sync will re-evaluate.",
        "source": ss.source,
        "source_url": ss.source_url,
    }


@router.post("/auth-status/reset-all")
async def reset_all_auth_health(db: AsyncSession = Depends(get_db)):
    """Reset auth health for ALL unhealthy subscription sources."""
    from app.models import SubscriptionSource
    from sqlalchemy import update

    result = await db.execute(
        update(SubscriptionSource)
        .where(SubscriptionSource.auth_healthy == False)  # noqa: E712
        .values(auth_healthy=True, auth_status=None, auth_error_reason=None, last_auth_checked_at=None)
        .returning(SubscriptionSource.id)
    )
    await db.commit()
    reset_ids = [str(r[0]) for r in result.all()]

    return {
        "status": "ok",
        "message": f"Reset auth health for {len(reset_ids)} source(s).",
        "reset_source_ids": reset_ids,
    }


# ── Backup & Restore ──

BACKUP_DIR = Path(settings.download_root) / ".backups"

ALL_BACKUP_CONTENTS = ["database", "gallerydl-config", "app-config", "download-archives", "library-metadata"]

BACKUP_CONTENT_LABELS = {
    "database": "PostgreSQL database dump",
    "gallerydl-config": "gallery-dl config + cookies",
    "app-config": "Application runtime config",
    "download-archives": "Download archive files (per-source SQLite)",
    "library-metadata": "Library metadata.json files",
}


def _parse_db_url(url: str) -> dict:
    """Parse DATABASE_URL into pg_dump-compatible components.

    NOTE: The returned dict includes ``password`` for temporary .pgpass
    files in subprocess calls. Callers MUST NOT log or serialize this dict.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "postgres",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "autogallery",
        "password": parsed.password or "",
        "dbname": parsed.path.lstrip("/") or "autogallery",
    }


def _escape_pgpass_field(value: str) -> str:
    """Escape a value for PostgreSQL .pgpass format."""
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _pg_env_with_passfile(tmpdir: str, db_info: dict) -> dict:
    """Return subprocess env using a temporary .pgpass file, not PGPASSWORD."""
    pgpass_path = os.path.join(tmpdir, ".pgpass")
    fields = [
        db_info["host"],
        db_info["port"],
        db_info["dbname"],
        db_info["user"],
        db_info["password"],
    ]
    with open(pgpass_path, "w", encoding="utf-8") as f:
        f.write(":".join(_escape_pgpass_field(str(field)) for field in fields) + "\n")
    os.chmod(pgpass_path, 0o600)
    env = os.environ.copy()
    env.pop("PGPASSWORD", None)
    env["PGPASSFILE"] = pgpass_path
    return env


def _estimate_component_sizes() -> dict[str, int]:
    """Estimate the size of each backup component (bytes)."""
    sizes: dict[str, int] = {}
    # Database: rough estimate from pg_dump (we can't know exactly without running it)
    sizes["database"] = 0  # Will be measured during actual backup

    # gallery-dl config
    config_src = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"))
    if config_src.exists():
        sizes["gallerydl-config"] = sum(f.stat().st_size for f in config_src.rglob("*") if f.is_file())
    else:
        sizes["gallerydl-config"] = 0

    # App config
    app_src = Path(os.environ.get("APP_CONFIG_ROOT", "/app-config"))
    if app_src.exists():
        sizes["app-config"] = sum(f.stat().st_size for f in app_src.rglob("*") if f.is_file())
    else:
        sizes["app-config"] = 0

    # Download archives
    dl_root = Path(settings.download_root)
    sizes["download-archives"] = sum(
        af.stat().st_size for af in dl_root.glob("archive-*.sqlite3") if af.is_file())

    # Library metadata
    lib_root = Path(settings.library_root)
    if lib_root.exists():
        sizes["library-metadata"] = sum(
            f.stat().st_size for f in lib_root.rglob("metadata.json") if f.is_file())
    else:
        sizes["library-metadata"] = 0

    return sizes


@router.get("/backup/estimate")
async def estimate_backup_sizes():
    """Return estimated sizes for each backup component.

    Offloaded to a thread — _estimate_component_sizes walks lib_root for
    metadata.json files, a full-tree traversal that must not block the loop.
    """
    sizes = await asyncio.to_thread(_estimate_component_sizes)
    return {"components": {k: round(v / 1024, 1) for k, v in sizes.items()}}


@router.post("/backup")
async def create_backup(data: dict | None = None):
    """Create a system backup with optional content selection.

    Body (optional): {contents: ["database", "gallerydl-config", ...]}
    Defaults to all components if not specified.

    The whole body is blocking (pg_dump, copytree, rglob, tar.gz) and is run
    off the event loop so a backup doesn't freeze the gallery for everyone.
    """
    return await asyncio.to_thread(_create_backup_sync, data)


def _create_backup_sync(data: dict | None = None):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"auto-gallery-backup_{ts}.tar.gz"
    filepath = BACKUP_DIR / filename

    selected = (data or {}).get("contents", list(ALL_BACKUP_CONTENTS))
    selected = [c for c in selected if c in ALL_BACKUP_CONTENTS]
    if not selected:
        selected = list(ALL_BACKUP_CONTENTS)

    db_info = _parse_db_url(settings.database_url)
    tmpdir = tempfile.mkdtemp(prefix="ag-backup-")
    sizes: dict[str, int] = {}

    try:
        # 1. PostgreSQL dump
        if "database" in selected:
            dump_path = os.path.join(tmpdir, "database.sql")
            env = _pg_env_with_passfile(tmpdir, db_info)
            result = subprocess.run(
                ["pg_dump", "-h", db_info["host"], "-p", db_info["port"], "-U", db_info["user"],
                 "-d", db_info["dbname"], "--no-owner", "--no-acl", "-f", dump_path],
                capture_output=True, text=True, env=env, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"Database dump failed: {result.stderr[:500]}")
            sizes["database"] = os.path.getsize(dump_path)

        # 2. gallery-dl config
        if "gallerydl-config" in selected:
            config_src = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"))
            config_dst = os.path.join(tmpdir, "gallerydl-config")
            if config_src.exists():
                shutil.copytree(str(config_src), config_dst, symlinks=False, ignore_dangling_symlinks=True,
                                ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".git"))

        # 3. App config
        if "app-config" in selected:
            app_config_src = Path(os.environ.get("APP_CONFIG_ROOT", "/app-config"))
            app_config_dst = os.path.join(tmpdir, "app-config")
            if app_config_src.exists():
                shutil.copytree(str(app_config_src), app_config_dst, symlinks=False, ignore_dangling_symlinks=True,
                                ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".git"))

        # 4. Download archives
        if "download-archives" in selected:
            dl_root = Path(settings.download_root)
            archives_dst = os.path.join(tmpdir, "download-archives")
            os.makedirs(archives_dst, exist_ok=True)
            for af in dl_root.glob("archive-*.sqlite3"):
                shutil.copy2(str(af), os.path.join(archives_dst, af.name))
                sizes[f"archive:{af.stem}"] = af.stat().st_size

        # 5. Library metadata
        if "library-metadata" in selected:
            lib_root = Path(settings.library_root)
            lib_dst = os.path.join(tmpdir, "library-metadata")
            if lib_root.exists():
                for mf in lib_root.rglob("metadata.json"):
                    rel = mf.relative_to(lib_root)
                    dest = Path(lib_dst) / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(mf), str(dest))

        # Manifest
        manifest = {
            "created_at": ts,
            "version": "0.2.0",
            "contents": selected,
            "component_sizes": {k: v for k, v in sizes.items()},
        }
        with open(os.path.join(tmpdir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)

        # Create tar.gz
        with tarfile.open(filepath, "w:gz") as tar:
            for item in os.listdir(tmpdir):
                tar.add(os.path.join(tmpdir, item), arcname=item)

        file_size = os.path.getsize(filepath)
        logger.info("Backup created: %s (%.1f MB) contents=%s", filename, file_size / 1024 / 1024, selected)

        # Keep last 10 backups
        existing = sorted(BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"))
        for old in existing[:-10]:
            old.unlink()

        return {
            "status": "ok",
            "filename": filename,
            "size_bytes": file_size,
            "size_mb": round(file_size / 1024 / 1024, 1),
            "contents": selected,
            "component_sizes": {k: round(v / 1024, 1) for k, v in sizes.items()},
        }

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# restore_backup is defined below with _safe_extract_tar, .pgpass handling,
# and confirm=DELETE-EVERYTHING guard.  See the second definition in this file.


def _validate_backup_filename(filename: str) -> Path:
    """Resolve and validate a backup filename stays within BACKUP_DIR."""
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not (filename.startswith("auto-gallery-backup_") and filename.endswith(".tar.gz")):
        raise HTTPException(status_code=400, detail="Invalid filename")
    target = (BACKUP_DIR / filename).resolve()
    try:
        target.relative_to(BACKUP_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return target


@router.get("/backup/download")
async def download_backup(filename: str | None = None):
    """Download a backup file. If filename not specified, returns the latest."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"))
    if not existing:
        return {"status": "error", "message": "No backups available"}
    target = _validate_backup_filename(filename) if filename else existing[-1]
    if not target.exists():
        return {"status": "error", "message": f"Backup not found: {filename}"}
    return FileResponse(
        str(target), media_type="application/gzip", filename=target.name,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'})


@router.delete("/backup/{filename}")
async def delete_backup(filename: str):
    """Delete a specific backup file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = _validate_backup_filename(filename)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Backup not found: {filename}")
    target.unlink()
    return {"status": "ok", "message": f"Deleted {filename}"}


@router.get("/backup/list")
async def list_backups():
    """List available backup files."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(BACKUP_DIR.glob("auto-gallery-backup_*.tar.gz"), reverse=True)
    result = []
    for f in existing:
        stat = f.stat()
        result.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / 1024 / 1024, 1),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"backups": result}


def _safe_extract_tar(tar_path: str, dest_dir: str) -> None:
    """Extract a tar.gz file, validating all member paths stay within dest_dir."""
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = os.path.join(dest_dir, member.name)
            resolved = os.path.realpath(member_path)
            resolved_dest = os.path.realpath(dest_dir)
            if os.path.commonpath([resolved, resolved_dest]) != resolved_dest:
                logger.warning("Rejected tar member outside dest: %s -> %s", member.name, resolved)
                continue
            if member.isdir():
                os.makedirs(resolved, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(resolved), exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src, open(resolved, "wb") as dst:
                    dst.write(src.read())


def _do_restore(upload_path: str, tmpdir: str) -> dict:
    """Blocking restore work (tar extract, psql subprocesses, config copy).
    Runs in a thread so a restore doesn't freeze the event loop."""
    extract_dir = os.path.join(tmpdir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    _safe_extract_tar(upload_path, extract_dir)

    manifest_path = os.path.join(extract_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"status": "error", "message": "Invalid backup: no manifest.json found"}

    results = []

    # 1. Restore database
    dump_path = os.path.join(extract_dir, "database.sql")
    if os.path.exists(dump_path):
        db = _parse_db_url(settings.database_url)
        env = _pg_env_with_passfile(tmpdir, db)
        result = subprocess.run(
            ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["dbname"],
             "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if result.returncode != 0:
            logger.error("Schema reset failed: %s", result.stderr)
            results.append({"item": "database", "status": "error", "error": result.stderr[:200]})
        else:
            result = subprocess.run(
                ["psql", "-h", db["host"], "-p", db["port"], "-U", db["user"], "-d", db["dbname"],
                 "-f", dump_path],
                capture_output=True, text=True, env=env, timeout=120,
            )
            if result.returncode == 0:
                results.append({"item": "database", "status": "restored"})
                logger.info("Database restored from backup")
            else:
                logger.error("DB restore failed: %s", result.stderr)
                results.append({"item": "database", "status": "error", "error": result.stderr[:200]})

    # 2. Restore config files
    for src_name, dst_env in [("gallerydl-config", "GALLERYDL_CONFIG_ROOT"),
                               ("app-config", "APP_CONFIG_ROOT")]:
        src = os.path.join(extract_dir, src_name)
        if os.path.exists(src):
            dst = os.environ.get(dst_env, f"/{src_name}")
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, symlinks=False, ignore_dangling_symlinks=True)
            results.append({"item": src_name, "status": "restored"})

    # 3. Restore download archives
    archives_src = os.path.join(extract_dir, "download-archives")
    if os.path.exists(archives_src):
        dl_root = str(settings.download_root)
        for af in os.listdir(archives_src):
            shutil.copy2(os.path.join(archives_src, af), os.path.join(dl_root, af))
        results.append({"item": "download-archives", "status": "restored"})

    return {"status": "ok", "results": results}


@router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...), confirm: str = ""):
    """Restore system from a backup file. THIS IS DESTRUCTIVE — replaces current data.

    Requires ``?confirm=DELETE-EVERYTHING`` to prevent accidental invocation.
    """
    if confirm != "DELETE-EVERYTHING":
        return {"status": "error", "message": "Add ?confirm=DELETE-EVERYTHING to proceed with restore"}
    if not file.filename or not file.filename.endswith(".tar.gz"):
        return {"status": "error", "message": "Invalid file: must be a .tar.gz backup"}

    logger.warning("Backup restore initiated (confirm=%s, file=%s)", confirm, file.filename)

    tmpdir = tempfile.mkdtemp(prefix="ag-restore-")
    try:
        # Save uploaded file with a fixed name (ignore user-supplied filename for safety)
        upload_path = os.path.join(tmpdir, "upload.tar.gz")
        with open(upload_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # Extract + DB restore + config copy are all blocking (tar, psql
        # subprocesses, copytree) — run off the event loop.
        return await asyncio.to_thread(_do_restore, upload_path, tmpdir)

    except Exception as e:
        logger.exception("Restore failed")
        return {"status": "error", "message": str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
