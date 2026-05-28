import asyncio
import logging
import os
import time

from fastapi import APIRouter
from app.auth import RequireAdmin

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequireAdmin])

# ── Storage stats TTL cache ────────────────────────────────────────────────────
_storage_cache: dict | None = None
_storage_cache_ts: float = 0.0
_STORAGE_CACHE_TTL = 60.0  # seconds


def _dir_size(path: str) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _dir_size(entry.path)
    except (OSError, PermissionError) as e:
        logger.debug("Cannot scan %s: %s", path, e)
    return total


def _count_files(path: str) -> int:
    count = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                count += 1
            elif entry.is_dir(follow_symlinks=False):
                count += _count_files(entry.path)
    except (OSError, PermissionError) as e:
        logger.debug("Cannot count files in %s: %s", path, e)
    return count


@router.get("/system/storage")
async def storage_stats():
    global _storage_cache, _storage_cache_ts
    now = time.monotonic()
    if _storage_cache is not None and (now - _storage_cache_ts) < _STORAGE_CACHE_TTL:
        return _storage_cache

    dl_root = settings.download_root
    lib_root = settings.library_root

    dl_size, lib_size, dl_files, lib_files = await asyncio.gather(
        asyncio.to_thread(_dir_size, dl_root),
        asyncio.to_thread(_dir_size, lib_root),
        asyncio.to_thread(_count_files, dl_root),
        asyncio.to_thread(_count_files, lib_root),
    )

    # Disk usage for the mount point
    try:
        stat = os.statvfs(dl_root)
        total_bytes = stat.f_frsize * stat.f_blocks
        free_bytes = stat.f_frsize * stat.f_bavail
        used_bytes = total_bytes - free_bytes
    except (OSError, PermissionError):
        total_bytes = 0
        free_bytes = 0
        used_bytes = 0

    result = {
        "downloads": {"path": dl_root, "size_bytes": dl_size, "file_count": dl_files},
        "library": {"path": lib_root, "size_bytes": lib_size, "file_count": lib_files},
        "disk": {"total_bytes": total_bytes, "free_bytes": free_bytes, "used_bytes": used_bytes},
    }
    _storage_cache = result
    _storage_cache_ts = now
    return result


@router.get("/system/queue-stats")
async def queue_stats():
    try:
        import redis as redis_lib
        from rq import Queue
        from rq.registry import FailedJobRegistry
        from app.config import settings as app_settings

        r = redis_lib.from_url(app_settings.redis_url)
        default_q = Queue(connection=r)
        scheduled_q = Queue(name="scheduled", connection=r)
        failed_reg = FailedJobRegistry(queue=default_q)
        sched_failed_reg = FailedJobRegistry(queue=scheduled_q)

        return {
            "default_queue": len(default_q),
            "scheduled_queue": len(scheduled_q),
            "failed_jobs": failed_reg.count + sched_failed_reg.count,
        }
    except Exception:
        logger.warning("Failed to fetch queue stats", exc_info=True)
        return {"default_queue": -1, "scheduled_queue": -1, "failed_jobs": -1}


@router.get("/system/logs")
async def get_logs(limit: int = 200, level: str | None = None, name: str | None = None):
    """Get recent application log entries from the in-memory ring buffer."""
    from app.services.log_buffer import get_recent, MAX_ENTRIES
    entries = get_recent(limit=min(limit, MAX_ENTRIES), level=level, name_filter=name)
    levels = sorted(set(e["level"] for e in entries), key=lambda x: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].index(x) if x in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] else 0)
    return {"entries": entries, "total": len(entries), "levels": levels}


@router.post("/system/clear-failed-jobs")
async def clear_failed_jobs():
    """Remove all failed jobs from Redis registries."""
    import redis as redis_lib
    from rq import Queue
    from rq.registry import FailedJobRegistry
    from app.config import settings as app_settings

    r = redis_lib.from_url(app_settings.redis_url)
    total = 0
    for qname in ["default", "scheduled"]:
        q = Queue(name=qname, connection=r)
        reg = FailedJobRegistry(queue=q)
        for job_id in reg.get_job_ids():
            reg.remove(job_id)
            total += 1
    return {"status": "ok", "message": f"Removed {total} failed jobs from Redis"}
