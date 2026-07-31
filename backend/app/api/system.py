import asyncio
import logging
import os
import shutil
import urllib.request
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from app.auth import RequirePermission
from sqlalchemy import and_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.jobs.subscription_sync import schedule_decision_snapshot
from app.models.creator import Creator
from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.work import Work
from app.models.work_source import WorkSource
from app.providers import registry
from app.services.settings import get_download_defaults, get_scheduler_config
from app.services.sync_outcome import download_job_outcome

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python < 3.9

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[RequirePermission("system")])
# Scheduler/queue operations belong to the `tasks` module per spec §A2
# (jobs/scheduler/notifications/tasks -> tasks), not `system`.
tasks_ops_router = APIRouter(dependencies=[RequirePermission("tasks")])

DOWNLOAD_RUNNING_STATUSES = {"enqueued", "downloading", "downloaded", "importing"}
IMPORT_RUNNING_STATUSES = {"enqueued", "running"}
FAILED_STATUSES = {"failed", "stale"}
QUEUE_NAMES = (
    "default", "downloads", "imports", "operations", "scheduled",
    "downloads:pixiv", "downloads:danbooru", "downloads:iwara",
    "downloads:weibo", "downloads:bilibili", "downloads:pinterest", "downloads:lofter",
)

# ── Storage stats TTL cache ────────────────────────────────────────────────────
_storage_cache: dict | None = None
_storage_cache_ts: float = 0.0
_STORAGE_CACHE_TTL = 60.0  # seconds


def _dir_size(
    path: str,
    seen_inodes: set[tuple[int, int]] | None = None,
) -> int:
    """Return physical file bytes, counting hard-linked inodes once."""
    if seen_inodes is None:
        seen_inodes = set()
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                inode = (stat.st_dev, stat.st_ino)
                if inode not in seen_inodes:
                    seen_inodes.add(inode)
                    total += stat.st_size
            elif entry.is_dir(follow_symlinks=False):
                total += _dir_size(entry.path, seen_inodes)
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


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _excerpt(value: str | None, limit: int = 240) -> str | None:
    if not value:
        return None
    clean = value.strip()
    return clean[:limit] if clean else None


def _storage_risk(storage: dict) -> str:
    disk = storage.get("disk") or {}
    total = disk.get("total_bytes") or 0
    free = disk.get("free_bytes") or 0
    if total <= 0:
        return "unknown"
    free_percent = (free / total) * 100
    if free_percent < 5:
        return "critical"
    if free_percent < 15:
        return "warning"
    return "ok"


async def _queue_stats_payload() -> dict:
    try:
        import redis as redis_lib
        from rq import Queue
        from rq.registry import FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry

        r = redis_lib.from_url(settings.redis_url)
        queues = {}
        for qname in QUEUE_NAMES:
            q = Queue(connection=r) if qname == "default" else Queue(name=qname, connection=r)
            scheduled_registry = ScheduledJobRegistry(queue=q)
            started_registry = StartedJobRegistry(queue=q)
            failed_registry = FailedJobRegistry(queue=q)
            queues[qname] = {
                "queued": len(q),
                "scheduled": scheduled_registry.count,
                "started": started_registry.count,
                "failed": failed_registry.count,
            }

        scheduled_q = Queue(name="scheduled", connection=r)
        scheduled_registry = ScheduledJobRegistry(queue=scheduled_q)
        sync_jobs = []
        for job_id in scheduled_registry.get_job_ids():
            job = scheduled_q.fetch_job(job_id)
            if job and "sync_subscriptions" in (job.func_name or ""):
                sync_jobs.append((job, scheduled_registry.get_scheduled_time(job)))
        next_sync_scan_at = None
        if sync_jobs:
            _job, next_sync_scan_at = min(sync_jobs, key=lambda item: item[1])
            next_sync_scan_at = _iso(next_sync_scan_at)
        return {
            "default_queue": queues["default"]["queued"],
            "scheduled_queue": queues["scheduled"]["queued"] + queues["scheduled"]["scheduled"],
            "failed_jobs": sum(item["failed"] for item in queues.values()),
            "started_jobs": sum(item["started"] for item in queues.values()),
            "next_sync_scan_at": next_sync_scan_at,
            "queues": queues,
        }
    except Exception:
        logger.warning("Failed to fetch Redis queue stats", exc_info=True)
        return {
            "default_queue": -1,
            "scheduled_queue": -1,
            "failed_jobs": -1,
            "started_jobs": -1,
            "next_sync_scan_at": None,
            "queues": {},
        }


async def _quick_service_health(db: AsyncSession) -> dict:
    services = {"backend": "up", "postgres": "unknown", "redis": "unknown", "meilisearch": "unknown", "gallery-dl": "unknown"}
    try:
        await db.execute(select(func.now()))
        services["postgres"] = "up"
    except Exception:
        services["postgres"] = "down"
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.redis_url)
        services["redis"] = "up" if r.ping() else "down"
    except Exception:
        services["redis"] = "down"
    try:
        def _check_meili() -> str:
            with urllib.request.urlopen(f"{settings.meili_url.rstrip('/')}/health", timeout=2) as response:
                return "up" if 200 <= response.status < 300 else "down"
        services["meilisearch"] = await asyncio.to_thread(_check_meili)
    except Exception:
        services["meilisearch"] = "down"
    services["gallery-dl"] = "up" if shutil.which("gallery-dl") else "missing"
    return services


async def _count_statuses(db: AsyncSession, model, statuses: set[str]) -> int:
    result = await db.execute(select(func.count(model.id)).where(model.status.in_(statuses)))
    return int(result.scalar() or 0)


def _provider_state(source: str, source_url: str | None) -> dict:
    try:
        provider = registry.get(source)
    except KeyError:
        return {
            "provider_display_name": source,
            "can_download": False,
            "url_valid": False,
            "skip_reason": "unknown_provider",
        }
    normalized_url = provider.normalize_url(source_url) if source_url else None
    candidate_url = normalized_url or source_url
    url_valid = bool(candidate_url and provider.validate_url(candidate_url))
    return {
        "provider_display_name": provider.display_name,
        "can_download": bool(provider.capabilities.can_download),
        "url_valid": url_valid,
        "skip_reason": None,
    }


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


@tasks_ops_router.get("/system/queue-stats")
async def queue_stats():
    try:
        from app.database import async_session

        queue_payload = await _queue_stats_payload()
        async with async_session() as db:
            scheduler_config = await get_scheduler_config(db)

        return {
            "default_queue": queue_payload["default_queue"],
            "scheduled_queue": queue_payload["scheduled_queue"],
            "failed_jobs": queue_payload["failed_jobs"],
            "scheduler_enabled": bool(scheduler_config.get("scheduler_enabled", True)),
            "scheduler_mode": scheduler_config.get("schedule_mode", "interval"),
            "scheduler_timezone": scheduler_config.get("timezone", "UTC"),
            "scheduled_times": scheduler_config.get("scheduled_times", ""),
            "scheduler_scan_interval_minutes": int(scheduler_config.get("scheduler_scan_interval_minutes", 60)),
            "next_sync_scan_at": queue_payload["next_sync_scan_at"],
            "queues": queue_payload["queues"],
        }
    except Exception:
        logger.warning("Failed to fetch queue stats", exc_info=True)
        return {"default_queue": -1, "scheduled_queue": -1, "failed_jobs": -1, "scheduler_enabled": True}


async def _get_proxy_health_summary() -> dict:
    """Read proxy health from Redis keys set by download pre-flight checks."""
    sources = ["pixiv", "danbooru", "iwara", "weibo", "bilibili", "pinterest", "lofter"]
    result = {}
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        for source in sources:
            data = r.hgetall(f"proxy:health:{source}")
            if data:
                result[source] = {
                    "status": data.get(b"status", b"unknown").decode(),
                    "last_check": data.get(b"last_check", b"").decode(),
                    "warnings": data.get(b"warnings", b"").decode(),
                }
    except Exception:
        pass
    return {"sources": result}


async def _count_active_rebuilds() -> int:
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        active = r.get("library:rebuild:active")
        if not active:
            return 0
        from app.services.operations import get_operation_status
        jid = active.decode() if isinstance(active, bytes) else active
        status = get_operation_status(jid)
        return 1 if status and status.get("status") == "running" else 0
    except Exception:
        return 0


_workbench_cache: dict | None = None
_workbench_cache_ts: float = 0.0
_WORKBENCH_CACHE_TTL = 10.0


@router.get("/system/workbench")
async def workbench_summary(
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Read-only dashboard aggregation for the live admin workbench."""
    global _workbench_cache, _workbench_cache_ts
    _now_mono = time.monotonic()
    if (
        not refresh
        and _workbench_cache is not None
        and (_now_mono - _workbench_cache_ts) < _WORKBENCH_CACHE_TTL
    ):
        return _workbench_cache

    now = datetime.now(timezone.utc)
    # O(1) disk stats via statvfs instead of recursive filesystem scans
    try:
        stat = os.statvfs(settings.download_root)
        disk_total_bytes = stat.f_frsize * stat.f_blocks
        disk_free_bytes = stat.f_frsize * stat.f_bavail
        disk_used_bytes = disk_total_bytes - disk_free_bytes
        disk_used_percent = round((disk_used_bytes / disk_total_bytes) * 100, 1) if disk_total_bytes else None
        disk_free_percent = round((disk_free_bytes / disk_total_bytes) * 100, 1) if disk_total_bytes else None
    except (OSError, PermissionError):
        disk_total_bytes = disk_free_bytes = disk_used_bytes = 0
        disk_used_percent = disk_free_percent = None
    storage_risk = "unknown"
    if disk_total_bytes > 0:
        free_pct = (disk_free_bytes / disk_total_bytes) * 100
        storage_risk = "critical" if free_pct < 5 else "warning" if free_pct < 15 else "ok"
    queue_payload = await _queue_stats_payload()
    scheduler_config = await get_scheduler_config(db)
    download_defaults = await get_download_defaults(db)
    stale_cutoff = now - timedelta(seconds=int(download_defaults.get("timeout_seconds", 600)) * 2)

    active_download_count = await _count_statuses(db, DownloadJob, DOWNLOAD_RUNNING_STATUSES)
    failed_download_count = await _count_statuses(db, DownloadJob, FAILED_STATUSES)
    stale_download_rows = await db.execute(
        select(func.count(DownloadJob.id)).where(
            and_(DownloadJob.status == "downloading", DownloadJob.created_at < stale_cutoff)
        )
    )
    stale_download_count = int(stale_download_rows.scalar() or 0)

    active_import_count = await _count_statuses(db, ImportJob, IMPORT_RUNNING_STATUSES)
    failed_import_count = await _count_statuses(db, ImportJob, FAILED_STATUSES)
    stale_import_rows = await db.execute(
        select(func.count(ImportJob.id)).where(
            and_(ImportJob.status == "running", ImportJob.created_at < stale_cutoff)
        )
    )
    stale_import_count = int(stale_import_rows.scalar() or 0)

    auth_unhealthy_rows = await db.execute(
        select(func.count(SubscriptionSource.id)).where(SubscriptionSource.auth_healthy == False)
    )
    auth_unhealthy_count = int(auth_unhealthy_rows.scalar() or 0)

    latest_download_rows = list((await db.execute(
        select(DownloadJob, Subscription, Creator)
        .join(Subscription, DownloadJob.subscription_id == Subscription.id)
        .join(Creator, Subscription.creator_id == Creator.id)
        .order_by(DownloadJob.created_at.desc())
        .limit(5)
    )).all())
    latest_import_rows = list((await db.execute(
        select(ImportJob, DownloadJob, Subscription, Creator)
        .join(DownloadJob, ImportJob.download_job_id == DownloadJob.id)
        .join(Subscription, DownloadJob.subscription_id == Subscription.id)
        .join(Creator, Subscription.creator_id == Creator.id)
        .order_by(ImportJob.created_at.desc())
        .limit(5)
    )).all())
    latest_works = list((await db.execute(
        select(Work).order_by(Work.created_at.desc()).limit(5)
    )).scalars().all())
    work_context: dict = {}
    if latest_works:
        work_context_rows = list((await db.execute(
            select(
                WorkSource.work_id,
                WorkSource.source,
                Creator.display_name,
                Creator.name,
            )
            .outerjoin(
                SourceCreator,
                and_(
                    SourceCreator.source == WorkSource.source,
                    SourceCreator.source_creator_id == WorkSource.source_creator_id,
                ),
            )
            .outerjoin(Creator, Creator.id == SourceCreator.creator_id)
            .where(WorkSource.work_id.in_([work.id for work in latest_works]))
            .order_by(WorkSource.created_at.asc())
        )).all())
        for work_id, source, creator_display_name, creator_name in work_context_rows:
            work_context.setdefault(
                work_id,
                {
                    "source": source,
                    "creator_name": creator_display_name or creator_name,
                },
            )
    successful_sync_rows = list((await db.execute(
        select(SubscriptionSource, Subscription, Creator)
        .join(Subscription, SubscriptionSource.subscription_id == Subscription.id)
        .join(Creator, Subscription.creator_id == Creator.id)
        .where(SubscriptionSource.last_synced_at.is_not(None))
        .order_by(SubscriptionSource.last_synced_at.desc())
        .limit(5)
    )).all())

    payload = {
        "updated_at": now.isoformat(),
        "queue": {
            "default": queue_payload["default_queue"],
            "scheduled": queue_payload["scheduled_queue"],
            "failed": queue_payload["failed_jobs"],
            "started": queue_payload["started_jobs"],
            "rebuild_active": await _count_active_rebuilds(),
            "active_download_count": active_download_count,
            "active_import_count": active_import_count,
            "failed_download_count": failed_download_count,
            "failed_import_count": failed_import_count,
            "stale_download_count": stale_download_count,
            "stale_import_count": stale_import_count,
            "stale_count": stale_download_count + stale_import_count,
        },
        "scheduler": {
            "enabled": bool(scheduler_config.get("scheduler_enabled", True)),
            "mode": scheduler_config.get("schedule_mode", "interval"),
            "timezone": scheduler_config.get("timezone", "UTC"),
            "scheduled_times": scheduler_config.get("scheduled_times", ""),
            "scan_interval_minutes": int(scheduler_config.get("scheduler_scan_interval_minutes", 60)),
            "next_scan_at": queue_payload["next_sync_scan_at"],
        },
        "proxy_health": await _get_proxy_health_summary(),
        "storage": {
            "disk_total_bytes": disk_total_bytes,
            "disk_free_bytes": disk_free_bytes,
            "disk_used_bytes": disk_used_bytes,
            "disk_used_percent": disk_used_percent,
            "disk_free_percent": disk_free_percent,
            "risk_level": storage_risk,
        },
        "health": await _quick_service_health(db),
        "attention": {
            "auth_unhealthy_count": auth_unhealthy_count,
            "failed_download_count": failed_download_count,
            "failed_import_count": failed_import_count,
            "stale_job_count": stale_download_count + stale_import_count,
            "low_disk_warning": storage_risk in {"warning", "critical"},
            "scheduler_disabled_warning": not bool(scheduler_config.get("scheduler_enabled", True)),
        },
        "recent": {
            "download_jobs": [{
                "id": str(j.id),
                "subscription_id": str(j.subscription_id),
                "subscription_source_id": str(j.subscription_source_id) if j.subscription_source_id else None,
                "source": j.source,
                "source_url": j.source_url,
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                "subscription_name": sub.name,
                "status": j.status,
                "pipeline_stage": j.pipeline_stage,
                "progress_data": j.progress_data,
                "outcome": download_job_outcome(j),
                "created_at": _iso(j.created_at),
                "updated_at": _iso(j.updated_at),
                "error_log_excerpt": _excerpt(j.error_log),
            } for j, sub, creator in latest_download_rows],
            "import_jobs": [{
                "id": str(j.id),
                "download_job_id": str(j.download_job_id),
                "source": download.source,
                "source_url": download.source_url,
                "subscription_id": str(sub.id),
                "subscription_name": sub.name,
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                "status": j.status,
                "progress_stage": j.progress_stage,
                "progress_works_done": j.progress_works_done,
                "progress_works_total": j.progress_works_total,
                "progress_data": j.progress_data,
                "created_at": _iso(j.created_at),
                "updated_at": _iso(j.updated_at),
                "error_log_excerpt": _excerpt(j.error_log),
            } for j, download, sub, creator in latest_import_rows],
            "works": [{
                "id": str(w.id),
                "title": w.title,
                "thumbnail_asset_id": str(w.thumbnail_asset_id) if w.thumbnail_asset_id else None,
                "source": (work_context.get(w.id) or {}).get("source"),
                "creator_name": (work_context.get(w.id) or {}).get("creator_name"),
                "created_at": _iso(w.created_at),
            } for w in latest_works],
            "successful_syncs": [{
                "source_id": str(ss.id),
                "subscription_id": str(sub.id),
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                "source": ss.source,
                "source_url": ss.source_url,
                "last_synced_at": _iso(ss.last_synced_at),
            } for ss, sub, creator in successful_sync_rows],
        },
    }
    _workbench_cache = payload
    _workbench_cache_ts = time.monotonic()
    return payload


@tasks_ops_router.get("/system/scheduler-decisions")
async def scheduler_decisions(db: AsyncSession = Depends(get_db)):
    """Explain current scheduler decisions at subscription-source granularity.

    This endpoint is deliberately read-only: it does not enqueue jobs or mutate
    last_attempted_at/last_synced_at.
    """
    config = await get_scheduler_config(db)
    tz_name = config.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    scheduler_enabled = bool(config.get("scheduler_enabled", True))

    rows = list((await db.execute(
        select(SubscriptionSource, Subscription, Creator)
        .join(Subscription, SubscriptionSource.subscription_id == Subscription.id)
        .join(Creator, Subscription.creator_id == Creator.id)
        .order_by(Creator.display_name, Creator.name, SubscriptionSource.source, SubscriptionSource.created_at.desc())
    )).all())

    items = []
    for ss, sub, creator in rows:
        provider_state = _provider_state(ss.source, ss.source_url)
        can_download = bool(provider_state["can_download"])
        url_valid = bool(provider_state["url_valid"])
        auth_healthy = ss.auth_healthy is not False
        decision = schedule_decision_snapshot(sub, config, ss.last_synced_at, ss.last_attempted_at, now, tz)
        due = bool(decision.get("due"))
        reason = str(decision.get("reason"))

        if not scheduler_enabled:
            due = False
            reason = "scheduler_disabled"
        elif not sub.is_active:
            due = False
            reason = "subscription_inactive"
        elif not sub.sync_enabled:
            due = False
            reason = "subscription_sync_disabled"
        elif not ss.is_enabled:
            due = False
            reason = "source_disabled"
        elif not auth_healthy:
            due = False
            reason = "auth_unhealthy"
        elif not can_download:
            due = False
            reason = provider_state["skip_reason"] or "provider_not_downloadable"
        elif not url_valid:
            due = False
            reason = "url_invalid"

        items.append({
            "subscription_id": str(sub.id),
            "subscription_name": sub.name,
            "subscription_active": sub.is_active,
            "subscription_sync_enabled": sub.sync_enabled,
            "creator_id": str(creator.id),
            "creator_name": creator.display_name or creator.name,
            "source_id": str(ss.id),
            "source": ss.source,
            "source_display_name": provider_state["provider_display_name"],
            "source_url": ss.source_url,
            "source_creator_id": ss.source_creator_id,
            "source_enabled": ss.is_enabled,
            "effective_mode": decision.get("mode") or sub.schedule_mode or config.get("schedule_mode", "interval"),
            "timezone": tz_name,
            "scheduled_times": sub.scheduled_times or config.get("scheduled_times", ""),
            "sync_interval_hours": sub.sync_interval_hours,
            "last_synced_at": _iso(ss.last_synced_at),
            "last_attempted_at": _iso(ss.last_attempted_at),
            "due": due,
            "decision": "due_now" if due else reason,
            "reason": reason,
            "next_due_at": decision.get("next_due_at"),
            "window_start": decision.get("window_start"),
            "window_end": decision.get("window_end"),
            "auth_healthy": auth_healthy,
            "url_valid": url_valid,
            "can_download": can_download,
        })

    return {
        "updated_at": now.isoformat(),
        "scheduler_enabled": scheduler_enabled,
        "timezone": tz_name,
        "items": items,
    }


@router.get("/system/logs")
async def get_logs(limit: int = 200, level: str | None = None, name: str | None = None):
    """Get recent application log entries from the in-memory ring buffer."""
    from app.services.log_buffer import get_recent, MAX_ENTRIES
    entries = get_recent(limit=min(limit, MAX_ENTRIES), level=level, name_filter=name)
    _LEVEL_ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    levels = sorted(set(e["level"] for e in entries), key=lambda x: _LEVEL_ORDER.index(x) if x in _LEVEL_ORDER else len(_LEVEL_ORDER))
    return {"entries": entries, "total": len(entries), "levels": levels}


@tasks_ops_router.post("/system/clear-failed-jobs")
async def clear_failed_jobs():
    """Remove all failed jobs from Redis registries."""
    import redis as redis_lib
    from rq import Queue
    from rq.registry import FailedJobRegistry
    from app.config import settings as app_settings

    r = redis_lib.from_url(app_settings.redis_url)
    total = 0
    for qname in QUEUE_NAMES:
        q = Queue(connection=r) if qname == "default" else Queue(name=qname, connection=r)
        reg = FailedJobRegistry(queue=q)
        for job_id in reg.get_job_ids():
            reg.remove(job_id, delete_job=True)
            total += 1
    return {"status": "ok", "message": f"Removed {total} failed jobs from Redis"}
