"""Low-impact scheduled maintenance for gallery-dl SQLite databases."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path

import redis as redis_lib

from app.config import settings
from app.services.backpressure import DOWNLOAD_QUEUE_NAMES
from app.services.heavy_io import resource_pressure_paused, try_heavy_io_slot
from app.services.queue_admission import (
    QueueAdmissionError,
    checked_enqueue_in,
    ensure_redis_enqueue_capacity,
)
from app.services.redis_client import get_redis

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+
    from backports.zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SQLITE_MAINTENANCE_SCHEDULE_KEY = "maintenance:sqlite:scheduled"
MAINTENANCE_HOUR = 3
MAINTENANCE_MINUTE = 30
HEAVY_QUEUE_NAMES = (
    *DOWNLOAD_QUEUE_NAMES,
    "imports",
    "operations",
    "maintenance",
)
VACUUM_MIN_FREE_RATIO = 0.20
VACUUM_MIN_RECLAIM_BYTES = 256 * 1024 * 1024


def _local_timezone():
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        return ZoneInfo("UTC")


def next_maintenance_delay(now: datetime | None = None) -> timedelta:
    tz = _local_timezone()
    current = now.astimezone(tz) if now else datetime.now(tz)
    target = datetime.combine(
        current.date(),
        time(MAINTENANCE_HOUR, MAINTENANCE_MINUTE),
        tzinfo=tz,
    )
    if target <= current:
        target += timedelta(days=1)
    return target - current


def ensure_sqlite_maintenance_scheduled(now: datetime | None = None) -> bool:
    """Single-flight schedule of the next local 03:30 maintenance run."""

    from rq import Queue

    redis = get_redis()
    ensure_redis_enqueue_capacity(redis)
    delay = next_maintenance_delay(now)
    # Cover a full additional two days so a delayed RQ scheduled job cannot be
    # duplicated by the hourly subscription scan.
    ttl = max(3600, int(delay.total_seconds()) + 172800)
    current = now.astimezone(_local_timezone()) if now else datetime.now(_local_timezone())
    target = (current + delay).isoformat()
    marker_token = uuid.uuid4().hex
    marker_value = f"v2:{marker_token}"
    if not redis.set(
        SQLITE_MAINTENANCE_SCHEDULE_KEY,
        marker_value,
        nx=True,
        ex=ttl,
    ):
        return False
    try:
        checked_enqueue_in(
            Queue(name="scheduled", connection=redis),
            delay,
            "app.jobs.sqlite_maintenance.run_sqlite_maintenance",
            marker_token,
            job_id=f"sqlite-maintenance-{marker_token}",
            job_timeout=14400,
            result_ttl=86400,
        )
    except Exception:
        _release_schedule_marker(redis, marker_token)
        raise
    logger.info("Scheduled SQLite maintenance for %s", target)
    return True


def _release_schedule_marker(redis, marker_token: str) -> bool:
    """Remove only the marker owned by one scheduled maintenance job."""

    expected = f"v2:{marker_token}"
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        return bool(redis.eval(
            script,
            1,
            SQLITE_MAINTENANCE_SCHEDULE_KEY,
            expected,
        ))
    except Exception:
        current = redis.get(SQLITE_MAINTENANCE_SCHEDULE_KEY)
        if isinstance(current, bytes):
            current = current.decode()
        if current != expected:
            return False
        return bool(redis.delete(SQLITE_MAINTENANCE_SCHEDULE_KEY))


def _heavy_queues_idle() -> tuple[bool, dict[str, int]]:
    from rq import Queue
    from rq.registry import StartedJobRegistry

    redis = get_redis()
    counts: dict[str, int] = {}
    for name in HEAVY_QUEUE_NAMES:
        queued = Queue(name=name, connection=redis).count
        started = StartedJobRegistry(name=name, connection=redis).count
        if queued or started:
            counts[name] = queued + started
    return not counts, counts


def _database_paths() -> list[Path]:
    root = Path(settings.download_root)
    # FileIndex is a legacy read-only migration source. PostgreSQL
    # StorageArtifact is the only live ledger; never maintain the old SQLite
    # file in the runtime schedule.
    return sorted(root.glob("archive-*.sqlite3"))


def _maintain_database(path: Path, *, full_vacuum: bool) -> dict[str, int | float | str]:
    connection = sqlite3.connect(str(path), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0] or 0)
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0] or 0)
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0] or 0)
        free_ratio = free_pages / page_count if page_count else 0.0
        reclaim_bytes = free_pages * page_size
        vacuum_eligible = (
            full_vacuum
            and free_ratio > VACUUM_MIN_FREE_RATIO
            and reclaim_bytes > VACUUM_MIN_RECLAIM_BYTES
        )
        mode = "vacuum" if vacuum_eligible else "optimize"
        connection.execute("VACUUM" if vacuum_eligible else "PRAGMA optimize")
        connection.commit()
        return {
            "mode": mode,
            "page_count": page_count,
            "free_pages": free_pages,
            "free_ratio": round(free_ratio, 4),
            "reclaim_bytes": reclaim_bytes,
        }
    finally:
        connection.close()


def run_sqlite_maintenance(marker_token: str | None = None) -> dict:
    return asyncio.run(run_sqlite_maintenance_async(marker_token=marker_token))


async def run_sqlite_maintenance_async(
    now: datetime | None = None,
    marker_token: str | None = None,
) -> dict:
    """Optimize daily; use full VACUUM only on Sunday while fully idle."""

    redis = get_redis()
    try:
        if marker_token:
            _release_schedule_marker(redis, marker_token)
        else:
            # One-time compatibility for jobs scheduled before marker tokens
            # were introduced. Never let a legacy job erase a v2 successor.
            current_marker = redis.get(SQLITE_MAINTENANCE_SCHEDULE_KEY)
            if isinstance(current_marker, bytes):
                current_marker = current_marker.decode()
            if current_marker and not str(current_marker).startswith("v2:"):
                redis.delete(SQLITE_MAINTENANCE_SCHEDULE_KEY)
    except Exception:
        logger.warning("Failed to clear current SQLite maintenance schedule marker", exc_info=True)
    current = (now or datetime.now(_local_timezone())).astimezone(_local_timezone())
    try:
        # Publish the successor before any optimize/VACUUM work. The same RQ
        # job retries during Redis protection, so a transient write failure
        # cannot break the daily maintenance chain.
        await asyncio.to_thread(ensure_sqlite_maintenance_scheduled, current)
    except QueueAdmissionError as exc:
        from rq import Retry

        logger.warning(
            "SQLite maintenance successor deferred by Redis admission code=%s",
            exc.code,
        )
        return Retry(max=1_000_000, interval=60)
    except (redis_lib.RedisError, OSError) as exc:
        from rq import Retry

        logger.warning(
            "SQLite maintenance successor deferred by Redis error=%s",
            type(exc).__name__,
        )
        return Retry(max=1_000_000, interval=60)

    full_vacuum = current.weekday() == 6
    result: dict = {
        "status": "skipped",
        "mode": "vacuum" if full_vacuum else "optimize",
        "databases": [],
        "details": {},
    }
    paused, snapshot = await resource_pressure_paused()
    if paused:
        result["reason"] = "resource_pressure"
        result["pressure_reasons"] = snapshot.get("reasons") or []
        return result
    vacuum_resources_normal = snapshot.get("controller_mode", "normal") == "normal"

    idle, busy_queues = await asyncio.to_thread(_heavy_queues_idle)
    if not idle:
        result["reason"] = "queues_busy"
        result["busy_queues"] = busy_queues
        return result

    async with try_heavy_io_slot("sqlite-maintenance", current.date().isoformat()) as lease:
        if lease is None:
            result["reason"] = "heavy_io_busy"
            return result
        paused, snapshot = await resource_pressure_paused()
        if paused:
            result["reason"] = "resource_pressure"
            result["pressure_reasons"] = snapshot.get("reasons") or []
            return result
        vacuum_resources_normal = (
            vacuum_resources_normal
            and snapshot.get("controller_mode", "normal") == "normal"
        )
        # Recheck after acquiring to close the admission race with workers.
        idle, busy_queues = await asyncio.to_thread(_heavy_queues_idle)
        if not idle:
            result["reason"] = "queues_busy"
            result["busy_queues"] = busy_queues
            return result
        for path in await asyncio.to_thread(_database_paths):
            try:
                details = await asyncio.to_thread(
                    _maintain_database,
                    path,
                    full_vacuum=full_vacuum and vacuum_resources_normal,
                )
                result["databases"].append(path.name)
                result["details"][path.name] = details
            except Exception:
                logger.warning("SQLite maintenance failed for %s", path, exc_info=True)
        result["status"] = "complete"
        return result
