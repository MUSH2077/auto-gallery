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
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.models.system_setting import SystemSetting
from app.services.redis_client import get_redis
from app.services import admin_data
from app.services.admin_data import ENTITIES, clear_entity_data

from ._routers import router

DEFAULT_DEDUP = {"source_level_enabled": False, "cross_source_enabled": False, "auto_merge": False, "phash_threshold": 8}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False}

_system_info_cache: dict | None = None
_system_info_cache_ts: float = 0.0
_SYSTEM_INFO_CACHE_TTL = 60.0
_system_info_lock = asyncio.Lock()

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

@router.post("/reset-settings")
async def reset_settings(db: AsyncSession = Depends(get_db)):
    """Reset all system settings to defaults."""
    return await clear_entity_data("settings", db)


# ── Scheduler ──
