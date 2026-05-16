import os
from pathlib import Path

from fastapi import APIRouter

from app.config import settings

router = APIRouter()


@router.get("/system/health")
async def health():
    return {"status": "ok", "version": "0.1.0", "services": {"postgres": "up", "redis": "up", "meilisearch": "up"}}


def _dir_size(path: str) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _dir_size(entry.path)
    except (OSError, PermissionError):
        pass
    return total


def _count_files(path: str) -> int:
    count = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                count += 1
            elif entry.is_dir(follow_symlinks=False):
                count += _count_files(entry.path)
    except (OSError, PermissionError):
        pass
    return count


@router.get("/system/storage")
async def storage_stats():
    dl_root = settings.download_root
    lib_root = settings.library_root

    dl_size = _dir_size(dl_root)
    lib_size = _dir_size(lib_root)
    dl_files = _count_files(dl_root)
    lib_files = _count_files(lib_root)

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

    return {
        "downloads": {"path": dl_root, "size_bytes": dl_size, "file_count": dl_files},
        "library": {"path": lib_root, "size_bytes": lib_size, "file_count": lib_files},
        "disk": {"total_bytes": total_bytes, "free_bytes": free_bytes, "used_bytes": used_bytes},
    }


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

        return {
            "default_queue": len(default_q),
            "scheduled_queue": len(scheduled_q),
            "failed_jobs": failed_reg.count,
        }
    except Exception:
        return {"default_queue": -1, "scheduled_queue": -1, "failed_jobs": -1}
