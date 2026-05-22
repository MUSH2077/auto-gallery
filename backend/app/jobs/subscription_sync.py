import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, and_

from app.config import settings
from app.database import async_session
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.download_job import DownloadJob

logger = logging.getLogger(__name__)

FALLBACK_INTERVAL_HOURS = 6
FALLBACK_SCAN_MINUTES = 60


async def _get_scheduler_config(db) -> dict:
    """Read scheduler configuration from system_settings table."""
    from app.models.system_setting import SystemSetting
    result = await db.execute(
        select(SystemSetting).where(SystemSetting.key == "subscription_defaults")
    )
    row = result.scalar_one_or_none()
    if row and row.value:
        return row.value
    return {}


def _should_sync_now(config: dict, last_synced_at, interval_hours: int) -> bool:
    """Check whether a sync is due, respecting schedule_mode."""
    if not last_synced_at:
        return True  # Never synced — always sync

    now = datetime.now(timezone.utc)
    mode = config.get("schedule_mode", "interval")

    if mode == "fixed_time":
        # Check if we've passed a scheduled time since last sync
        times_str = config.get("scheduled_times", "")
        if not times_str:
            # Fall back to interval if no times configured
            cutoff = now - timedelta(hours=interval_hours)
            return last_synced_at < cutoff

        scheduled = []
        for t in times_str.split(","):
            t = t.strip()
            try:
                parts = t.split(":")
                h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                scheduled.append((h, m))
            except ValueError:
                continue

        if not scheduled:
            cutoff = now - timedelta(hours=interval_hours)
            return last_synced_at < cutoff

        # Walk back day by day from today until we find the most recent
        # scheduled time that falls after last_synced_at.
        # This handles multi-day downtime correctly.
        max_lookback = 30  # days
        for days_back in range(max_lookback):
            day = now.date() - timedelta(days=days_back)
            best_time = None
            for h, m in scheduled:
                st = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
                if st <= now and (best_time is None or st > best_time):
                    best_time = st
            if best_time:
                if last_synced_at < best_time:
                    return True
                return False  # found the most recent time but it's before last sync

        # If we walked back 30 days and found nothing, fall back to interval
        cutoff = now - timedelta(hours=interval_hours)
        return last_synced_at < cutoff
        return False

    # interval mode (default)
    cutoff = now - timedelta(hours=interval_hours)
    return last_synced_at < cutoff


async def sync_subscriptions():
    logger.info("Starting subscription auto-sync scan")

    now = datetime.now(timezone.utc)
    jobs_created = 0

    async with async_session() as db:
        config = await _get_scheduler_config(db)
        default_interval = int(config.get("default_sync_interval_hours", FALLBACK_INTERVAL_HOURS))
        scan_minutes = int(config.get("scheduler_scan_interval_minutes", FALLBACK_SCAN_MINUTES))

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

            interval_hours = sub.sync_interval_hours or default_interval
            interval_cutoff = now - timedelta(hours=interval_hours)

            for ss in sub_sources:
                # Skip if auth is known to be unhealthy
                if ss.auth_healthy is False:
                    continue

                # Check if sync is due
                if not _should_sync_now(config, sub.last_synced_at, interval_hours):
                    continue

                # Check if there's a recent job for this subscription source
                recent = await db.execute(
                    select(DownloadJob).where(
                        and_(
                            DownloadJob.subscription_source_id == ss.id,
                            DownloadJob.status.in_(["pending", "downloading", "downloaded", "complete", "importing"]),
                            DownloadJob.created_at >= interval_cutoff,
                        )
                    ).limit(1)
                )
                if recent.scalar_one_or_none():
                    continue  # Already synced recently

                # Validate source URL
                if not ss.source_url:
                    continue

                from app.providers import registry
                provider = registry.get(ss.source)
                if not provider or not provider.capabilities.can_download:
                    continue
                if not provider.validate_url(ss.source_url):
                    continue

                # Create download job
                job = DownloadJob(
                    subscription_id=sub.id,
                    subscription_source_id=ss.id,
                    source=ss.source,
                    source_url=ss.source_url,
                    status="pending",
                )
                db.add(job)
                await db.flush()

                # Enqueue
                try:
                    import redis as redis_lib
                    from rq import Queue
                    r = redis_lib.from_url(settings.redis_url)
                    Queue(name="scheduled", connection=r).enqueue(
                        "app.jobs.download.run_download_job", str(job.id))
                except Exception as e:
                    logger.error("Failed to enqueue download job %s: %s", job.id, e)

                jobs_created += 1
                sub.last_synced_at = now
                logger.info("Auto-sync created download job %s for source=%s url=%s",
                            job.id, ss.source, ss.source_url)

        if jobs_created:
            await db.commit()

    mode = config.get("schedule_mode", "interval")
    logger.info("Auto-sync scan complete: %d jobs created (mode=%s, scan_every=%dm, default_interval=%dh)",
                jobs_created, mode, scan_minutes, default_interval)

    # Re-schedule for next scan
    try:
        import redis as redis_lib
        from rq import Queue
        r = redis_lib.from_url(settings.redis_url)
        Queue(connection=r).enqueue_in(
            timedelta(minutes=max(scan_minutes, 5)),
            "app.jobs.subscription_sync.sync_subscriptions",
        )
    except Exception as e:
        logger.error("Failed to re-schedule auto-sync: %s", e)
