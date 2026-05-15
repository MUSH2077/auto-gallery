import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, and_

from app.config import settings
from app.database import async_session
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.download_job import DownloadJob

logger = logging.getLogger(__name__)

# Minimum interval between auto-syncs per subscription source (hours)
MIN_SYNC_INTERVAL_HOURS = 6


async def sync_subscriptions():
    logger.info("Starting subscription auto-sync scan")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MIN_SYNC_INTERVAL_HOURS)
    jobs_created = 0

    async with async_session() as db:
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
                # Check if there's a recent job for this subscription source
                recent = await db.execute(
                    select(DownloadJob).where(
                        and_(
                            DownloadJob.subscription_source_id == ss.id,
                            DownloadJob.status.in_(["pending", "downloading", "downloaded"]),
                            DownloadJob.created_at >= cutoff,
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
                logger.info("Auto-sync created download job %s for source=%s url=%s",
                            job.id, ss.source, ss.source_url)

        if jobs_created:
            await db.commit()

    logger.info("Auto-sync scan complete: %d download jobs created", jobs_created)

    # Re-schedule for next scan
    try:
        import redis as redis_lib
        from rq import Queue
        r = redis_lib.from_url(settings.redis_url)
        Queue(connection=r).enqueue_in(
            timedelta(hours=1),
            "app.jobs.subscription_sync.sync_subscriptions",
        )
    except Exception as e:
        logger.error("Failed to re-schedule auto-sync: %s", e)
