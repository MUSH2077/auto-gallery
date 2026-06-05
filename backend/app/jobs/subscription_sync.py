import logging
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


def sync_subscriptions():
    asyncio.run(sync_subscriptions_async())


async def sync_subscriptions_async():
    with redis_lock("lock:subscription-sync-scan", ttl_seconds=300) as acquired:
        if not acquired:
            logger.info("Subscription auto-sync scan already running; skipping")
            return
        await _sync_subscriptions_locked()


async def _sync_subscriptions_locked():
    logger.info("Starting subscription auto-sync scan")
    jobs_created = 0
    skipped_count = 0

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

        # Find all active, sync-enabled subscriptions
        subs = await db.execute(
            select(Subscription).where(
                and_(Subscription.is_active == True, Subscription.sync_enabled == True)
            )
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

                result = await enqueue_subscription_source_sync(db, ss.id, trigger="scheduler")
                if result["status"] == "enqueued":
                    jobs_created += 1
                    logger.info("Auto-sync created download job",
                                extra={**log_context, **decision, "job_id": result["job_id"], "source_url": result.get("source_url")})
                else:
                    skipped_count += 1
                    logger.debug("Auto-sync skipped source after enqueue check",
                                 extra={**log_context, **decision, "skip_reason": result.get("skip_reason") or result})

    # Stale job detection: mark download jobs stuck "downloading" for too long
    try:
        async with async_session() as stale_db:
            dl_defaults = await get_download_defaults(stale_db)
            dl_timeout = int(dl_defaults.get("timeout_seconds", 600))
            stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=dl_timeout * 2)
            stale_result = await stale_db.execute(
                select(DownloadJob).where(
                    DownloadJob.status == "downloading",
                    DownloadJob.created_at < stale_cutoff,
                )
            )
            stale_jobs = stale_result.scalars().all()
            stale_repo = DownloadJobRepository(stale_db)
            for sj in stale_jobs:
                if sj.error_log:
                    error_log = sj.error_log + "\n[auto] Marked stale: stuck downloading for > 2x timeout"
                else:
                    error_log = "[auto] Marked stale: stuck downloading for > 2x timeout"
                await stale_repo.update_status(sj, "stale", error_log)
                logger.warning("Marked download job %s as stale (stuck downloading)", sj.id)
            if stale_jobs:
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

    mode = config.get("schedule_mode", "interval")
    logger.info("Auto-sync scan complete: %d jobs created, %d skipped (enabled=%s, mode=%s, timezone=%s, scan_every=%dm, default_interval=%dh)",
                jobs_created, skipped_count, config.get("scheduler_enabled", True), mode, config.get("timezone", "UTC"), scan_minutes, default_interval)

    # Re-schedule for next scan
    try:
        import redis as redis_lib
        from rq import Queue
        r = redis_lib.from_url(settings.redis_url)
        Queue(name="scheduled", connection=r).enqueue_in(
            timedelta(minutes=max(scan_minutes, 5)),
            sync_subscriptions,
        )
    except Exception as e:
        logger.error("Failed to re-schedule auto-sync: %s", e)
