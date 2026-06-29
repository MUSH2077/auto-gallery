import logging
import os
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import select, and_

from app.config import settings

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python < 3.9
from app.database import async_session
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.download_job import DownloadJob
from app.repositories.download_job import DownloadJobRepository
from app.services.locks import redis_lock
from app.services.redis_client import get_redis
from app.services.settings import get_download_defaults, get_scheduler_config
from app.services.subscription_enqueue import enqueue_subscription_source_sync

logger = logging.getLogger(__name__)

FALLBACK_INTERVAL_HOURS = 6
FALLBACK_SCAN_MINUTES = 60


def _as_tz(value: datetime | None, tz) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz)


def _parse_scheduled_times(times_str: str | None) -> list[tuple[int, int, int]]:
    scheduled: list[tuple[int, int, int]] = []
    for raw in (times_str or "").split(","):
        t = raw.strip()
        if not t:
            continue
        try:
            parts = t.split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            sec = int(parts[2]) if len(parts) > 2 else 0
            if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59:
                scheduled.append((h, m, sec))
        except (ValueError, IndexError):
            continue
    return scheduled


def _schedule_decision(
    sub,
    system_config: dict,
    last_synced_at,
    last_attempted_at,
    now: datetime,
    tz,
) -> dict:
    """Return a scheduler decision with a reason and optional fixed-time window."""
    mode = sub.schedule_mode or system_config.get("schedule_mode", "interval")

    if mode == "manual":
        return {"due": False, "mode": mode, "reason": "manual_mode"}

    if mode == "interval":
        if not last_synced_at:
            return {"due": True, "mode": mode, "reason": "never_synced_interval"}
        interval_hours = sub.sync_interval_hours or int(
            system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
        cutoff = now - timedelta(hours=interval_hours)
        last_synced = _as_tz(last_synced_at, tz)
        return {
            "due": bool(last_synced and last_synced < cutoff),
            "mode": mode,
            "reason": "interval_due" if last_synced and last_synced < cutoff else "interval_not_due",
        }

    if mode == "fixed_time":
        times_str = sub.scheduled_times or system_config.get("scheduled_times", "")
        scheduled = _parse_scheduled_times(times_str)
        if not scheduled:
            if not last_synced_at:
                return {"due": True, "mode": mode, "reason": "never_synced_interval_fallback"}
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            cutoff = now - timedelta(hours=interval_hours)
            last_synced = _as_tz(last_synced_at, tz)
            return {
                "due": bool(last_synced and last_synced < cutoff),
                "mode": mode,
                "reason": "interval_fallback_due" if last_synced and last_synced < cutoff else "interval_fallback_not_due",
            }

        scan_minutes = int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))
        window = timedelta(minutes=max(scan_minutes, 5) + 5)
        last_synced = _as_tz(last_synced_at, tz)
        last_attempted = _as_tz(last_attempted_at, tz)

        for day_offset in (0, -1):
            day = now.date() + timedelta(days=day_offset)
            for h, m, s_val in scheduled:
                st = datetime(day.year, day.month, day.day, h, m, s_val, tzinfo=tz)
                window_end = st + window
                if not (st <= now <= window_end):
                    continue
                payload = {
                    "mode": mode,
                    "window_start": st.isoformat(),
                    "window_end": window_end.isoformat(),
                    "scheduled_time": st.time().isoformat(),
                }
                if last_synced and last_synced >= st:
                    return {**payload, "due": False, "reason": "already_synced_in_window"}
                if last_attempted and last_attempted >= st:
                    return {**payload, "due": False, "reason": "already_attempted_in_window"}
                return {**payload, "due": True, "reason": "fixed_time_window_due"}

        return {"due": False, "mode": mode, "reason": "outside_fixed_time_window"}

    if not last_synced_at:
        return {"due": True, "mode": mode, "reason": "never_synced_interval_fallback"}
    interval_hours = sub.sync_interval_hours or int(
        system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
    cutoff = now - timedelta(hours=interval_hours)
    last_synced = _as_tz(last_synced_at, tz)
    return {
        "due": bool(last_synced and last_synced < cutoff),
        "mode": mode,
        "reason": "unknown_mode_interval_due" if last_synced and last_synced < cutoff else "unknown_mode_interval_not_due",
    }


def _should_sync_now(sub, system_config: dict, last_synced_at, now: datetime, tz, last_attempted_at=None) -> bool:
    """Check whether a sync is due, respecting per-subscription schedule strategy.

    Priority: subscription value > system default > hardcoded fallback.
    sub.schedule_mode=None means inherit from system_config.

    Modes: 'manual'=never, 'interval'=every N hours, 'fixed_time'=at scheduled times.
    """
    return bool(_schedule_decision(sub, system_config, last_synced_at, last_attempted_at, now, tz)["due"])


def _next_fixed_time_window(
    times_str: str | None,
    now: datetime,
    tz,
    scan_minutes: int,
) -> dict:
    scheduled = _parse_scheduled_times(times_str)
    if not scheduled:
        return {"next_due_at": None, "window_start": None, "window_end": None}

    window = timedelta(minutes=max(scan_minutes, 5) + 5)
    candidates = []
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for h, m, s_val in scheduled:
            start = datetime(day.year, day.month, day.day, h, m, s_val, tzinfo=tz)
            end = start + window
            if now <= end:
                candidates.append((start, end))
    if not candidates:
        return {"next_due_at": None, "window_start": None, "window_end": None}

    start, end = min(candidates, key=lambda item: item[0])
    return {
        "next_due_at": start.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }


def schedule_decision_snapshot(
    sub,
    system_config: dict,
    last_synced_at,
    last_attempted_at,
    now: datetime,
    tz,
) -> dict:
    """Read-only schedule explanation used by APIs and tests.

    This mirrors the enqueue scanner decision without mutating any source state.
    """
    decision = _schedule_decision(sub, system_config, last_synced_at, last_attempted_at, now, tz)
    mode = decision.get("mode") or sub.schedule_mode or system_config.get("schedule_mode", "interval")
    scan_minutes = int(system_config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))

    next_due_at = None
    window_start = decision.get("window_start")
    window_end = decision.get("window_end")

    if mode == "interval":
        if last_synced_at:
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            next_due = _as_tz(last_synced_at, tz) + timedelta(hours=interval_hours)
            next_due_at = next_due.isoformat()
        else:
            next_due_at = now.isoformat()
    elif mode == "fixed_time":
        fixed = _next_fixed_time_window(
            sub.scheduled_times or system_config.get("scheduled_times", ""),
            now,
            tz,
            scan_minutes,
        )
        next_due_at = fixed["next_due_at"]
        window_start = window_start or fixed["window_start"]
        window_end = window_end or fixed["window_end"]
    elif mode == "manual":
        next_due_at = None
    else:
        if last_synced_at:
            interval_hours = sub.sync_interval_hours or int(
                system_config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
            next_due_at = (_as_tz(last_synced_at, tz) + timedelta(hours=interval_hours)).isoformat()
        else:
            next_due_at = now.isoformat()

    return {
        **decision,
        "next_due_at": next_due_at,
        "window_start": window_start,
        "window_end": window_end,
    }


def sync_subscriptions():
    return asyncio.run(sync_subscriptions_async())


async def sync_subscriptions_async(parent_task_id=None):
    async with redis_lock("lock:subscription-sync-scan", ttl_seconds=300) as acquired:
        if not acquired:
            logger.info("Subscription auto-sync scan already running; skipping")
            return {"created": 0, "skipped": 1, "errors": 0, "rescheduled_at": None, "status": "skipped", "reason": "lock_busy"}
        return await _sync_subscriptions_locked(parent_task_id=parent_task_id)


async def _sync_subscriptions_locked(parent_task_id=None):
    logger.info("Starting subscription auto-sync scan")
    jobs_created = 0
    skipped_count = 0
    error_count = 0

    async with async_session() as db:
        config = await get_scheduler_config(db)

        # Use configured timezone for schedule evaluation
        tz_name = config.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)

        default_interval = int(config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
        scan_minutes = int(config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))
        scheduler_enabled = bool(config.get("scheduler_enabled", True))

        # Find active, sync-enabled subscriptions (batched: max 100 per scan)
        subs = await db.execute(
            select(Subscription).where(
                and_(Subscription.is_active == True, Subscription.sync_enabled == True)
            ).order_by(Subscription.last_synced_at.asc().nullsfirst()).limit(100)
        )
        subscriptions = subs.scalars().all()

        for sub in subscriptions:
            # Get enabled sources
            sources = await db.execute(
                select(SubscriptionSource).where(
                    and_(
                        SubscriptionSource.subscription_id == sub.id,
                        SubscriptionSource.is_enabled == True,
                    )
                )
            )
            sub_sources = sources.scalars().all()

            for ss in sub_sources:
                log_context = {"source_id": str(ss.id), "source": ss.source, "timezone": tz_name}
                if not scheduler_enabled:
                    skipped_count += 1
                    logger.debug("Auto-sync skipped source: scheduler disabled", extra={**log_context, "decision": "scheduler_disabled"})
                    continue

                # Skip if auth is known to be unhealthy
                if ss.auth_healthy is False:
                    skipped_count += 1
                    logger.debug("Auto-sync skipped source: auth unhealthy", extra={**log_context, "decision": "auth_unhealthy"})
                    continue

                # Check if sync is due (per-subscription strategy)
                decision = _schedule_decision(sub, config, ss.last_synced_at, ss.last_attempted_at, now, tz)
                if not decision["due"]:
                    skipped_count += 1
                    logger.debug("Auto-sync skipped source: not due", extra={**log_context, **decision})
                    continue

                result = await enqueue_subscription_source_sync(db, ss.id, trigger="scheduler", parent_task_id=parent_task_id)
                if result["status"] == "enqueued":
                    jobs_created += 1
                    logger.info("Auto-sync created download job",
                                extra={**log_context, **decision, "job_id": result["job_id"], "source_url": result.get("source_url")})
                else:
                    skipped_count += 1
                    if result.get("status") == "error":
                        error_count += 1
                    logger.debug("Auto-sync skipped source after enqueue check",
                                 extra={**log_context, **decision, "skip_reason": result.get("skip_reason") or result})

    # Stale job detection: check Redis heartbeat keys set by HeartbeatPublisher.
    # The heartbeat thread publishes to Redis pub/sub AND sets a TTL key
    # ``task:{job_id}:heartbeat_ts`` (90s TTL). If the key exists the worker
    # is alive; if missing, the worker has died or stalled.
    try:
        async with async_session() as stale_db:
            dl_defaults = await get_download_defaults(stale_db)
            dl_timeout = int(dl_defaults.get("timeout_seconds", 600))
            now = datetime.now(timezone.utc)
            created_at_fallback_cutoff = now - timedelta(seconds=dl_timeout * 2)

            # Find all jobs currently "downloading"
            result = await stale_db.execute(
                select(DownloadJob).where(DownloadJob.status == "downloading")
            )
            candidates = result.scalars().all()

            # Check Redis heartbeat keys for each candidate
            r = get_redis()
            stale_jobs = []
            if candidates:
                for job in candidates:
                    hb_key = f"task:{str(job.id)}:heartbeat_ts"
                    try:
                        if r.exists(hb_key):
                            continue  # heartbeat key exists → worker is alive
                    except Exception:
                        pass  # Redis error → fall through to created_at check

                    # No heartbeat key → check created_at fallback
                    created = job.created_at
                    if created is not None and created < created_at_fallback_cutoff:
                        stale_jobs.append(job)

            if stale_jobs:
                stale_repo = DownloadJobRepository(stale_db)
                for sj in stale_jobs:
                    if sj.error_log:
                        error_log = sj.error_log + "\n[auto] Marked stale: lost heartbeat (stuck downloading)"
                    else:
                        error_log = "[auto] Marked stale: lost heartbeat (stuck downloading)"
                    await stale_repo.update_status(sj, "stale", error_log)
                    logger.warning("Marked download job %s as stale (no heartbeat key in Redis)",
                                  sj.id)
                await stale_db.commit()
    except Exception:
        logger.debug("Stale job detection skipped", exc_info=True)

    # Periodic archive maintenance: VACUUM download archive SQLite files
    try:
        dl_root = Path(str(settings.download_root))
        for archive_file in dl_root.glob("archive-*.sqlite3"):
            try:
                conn = sqlite3.connect(str(archive_file))
                conn.execute("VACUUM")
                conn.close()
                logger.debug("VACUUMed download archive: %s", archive_file.name)
            except Exception:
                logger.debug("Failed to VACUUM archive %s", archive_file.name, exc_info=True)
    except Exception:
        logger.debug("Archive maintenance skipped", exc_info=True)

    # Vacuum file index alongside download archives
    try:
        from app.services.file_index import FileIndex, get_file_index
        fi_path = os.path.join(str(settings.download_root), ".file-index.sqlite3")
        if os.path.exists(fi_path):
            FileIndex(fi_path).vacuum()
            logger.debug("VACUUMed file index")
    except Exception:
        logger.debug("File index VACUUM skipped", exc_info=True)

    mode = config.get("schedule_mode", "interval")
    logger.info("Auto-sync scan complete: %d jobs created, %d skipped (enabled=%s, mode=%s, timezone=%s, scan_every=%dm, default_interval=%dh)",
                jobs_created, skipped_count, config.get("scheduler_enabled", True), mode, config.get("timezone", "UTC"), scan_minutes, default_interval)

    # Self-heal: link any orphaned source_creators to their creator so imported
    # works always surface on the creator page (cheap; usually 0 rows).
    try:
        from app.services.creator_reconcile import reconcile_unlinked_source_creators
        async with async_session() as relink_db:
            res = await reconcile_unlinked_source_creators(relink_db)
            if res["linked"]:
                await relink_db.commit()
                logger.info("Auto-sync relinked %d orphaned source_creators", res["linked"])
    except Exception:
        logger.debug("source_creator reconcile skipped", exc_info=True)

    # Re-schedule for next scan
    try:
        from rq import Queue
        next_scan_at = datetime.now(timezone.utc) + timedelta(minutes=max(scan_minutes, 5))
        Queue(name="scheduled", connection=get_redis()).enqueue_in(
            timedelta(minutes=max(scan_minutes, 5)),
            sync_subscriptions,
        )
        rescheduled_at = next_scan_at.isoformat()
    except Exception as e:
        logger.error("Failed to re-schedule auto-sync: %s", e)
        error_count += 1
        rescheduled_at = None

    return {
        "created": jobs_created,
        "skipped": skipped_count,
        "errors": error_count,
        "rescheduled_at": rescheduled_at,
        "status": "ok" if error_count == 0 else "partial_error",
    }
