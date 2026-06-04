import logging
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services.settings import get_download_defaults

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200
RUNNING_STATUSES = ("pending", "downloading", "downloaded", "importing")


async def refresh_subscription_last_synced_at(db: AsyncSession, subscription_id: UUID) -> datetime | None:
    result = await db.execute(
        select(func.max(SubscriptionSource.last_synced_at)).where(
            SubscriptionSource.subscription_id == subscription_id
        )
    )
    latest = result.scalar_one_or_none()
    sub = await db.get(Subscription, subscription_id)
    if sub:
        sub.last_synced_at = latest
        await db.flush()
    return latest


async def mark_source_sync_success(
    db: AsyncSession,
    subscription_source_id: UUID,
    when: datetime | None = None,
) -> None:
    when = when or datetime.now(timezone.utc)
    ss = await db.get(SubscriptionSource, subscription_source_id)
    if not ss:
        return
    ss.last_synced_at = when
    await refresh_subscription_last_synced_at(db, ss.subscription_id)


async def _latest_job_for_source(db: AsyncSession, source_id: UUID) -> DownloadJob | None:
    result = await db.execute(
        select(DownloadJob)
        .where(DownloadJob.subscription_source_id == source_id)
        .order_by(DownloadJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def enqueue_subscription_source_sync(
    db: AsyncSession,
    subscription_source_id: UUID,
    trigger: str = "manual",
    force: bool = False,
) -> dict:
    now = datetime.now(timezone.utc)
    ss = await db.get(SubscriptionSource, subscription_source_id)
    if not ss:
        return {"status": "skipped", "source_id": str(subscription_source_id), "skip_reason": "source_not_found"}

    sub = await db.get(Subscription, ss.subscription_id)
    if not sub:
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": "subscription_not_found"}
    if not force and not ss.is_enabled:
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": "source_disabled"}
    if not force and ss.auth_healthy is False:
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": "auth_unhealthy"}
    if not ss.source_url:
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": "source_url_empty"}

    try:
        provider = registry.get(ss.source)
    except KeyError:
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": f"unknown_provider:{ss.source}"}
    if not provider.capabilities.can_download:
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": "provider_not_downloadable"}

    normalized_url = provider.normalize_url(ss.source_url) or ss.source_url
    if not provider.validate_url(normalized_url):
        return {"status": "skipped", "source_id": str(ss.id), "skip_reason": "url_invalid"}
    if normalized_url != ss.source_url:
        ss.source_url = normalized_url

    running = await db.execute(
        select(DownloadJob)
        .where(
            DownloadJob.subscription_source_id == ss.id,
            DownloadJob.status.in_(RUNNING_STATUSES),
        )
        .order_by(DownloadJob.created_at.desc())
        .limit(1)
    )
    running_job = running.scalar_one_or_none()
    if running_job:
        return {
            "status": "skipped",
            "source_id": str(ss.id),
            "skip_reason": "already_running",
            "job_id": str(running_job.id),
        }

    if trigger == "scheduler" and not force:
        latest = await _latest_job_for_source(db, ss.id)
        latest_created_at = _as_utc(latest.created_at) if latest else None
        if latest and latest_created_at and latest.status in ("failed", "stale"):
            defaults = await get_download_defaults(db)
            backoff = int(defaults.get("retry_backoff_base_seconds", 60))
            max_retries = int(defaults.get("max_retries", 3))
            backoff_until = latest_created_at + timedelta(seconds=max(backoff, 1) * max(max_retries, 1))
            if now < backoff_until:
                return {
                    "status": "skipped",
                    "source_id": str(ss.id),
                    "skip_reason": "recent_failure_backoff",
                    "latest_job_id": str(latest.id),
                }

    job = DownloadJob(
        subscription_id=sub.id,
        subscription_source_id=ss.id,
        source=ss.source,
        source_url=normalized_url,
        status="pending",
    )
    ss.last_attempted_at = now
    db.add(job)
    await db.flush()

    try:
        import redis as redis_lib
        from rq import Queue

        r = redis_lib.from_url(settings.redis_url)
        Queue(connection=r).enqueue(
            "app.jobs.download.run_download_job",
            str(job.id),
            job_timeout=RQ_JOB_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Failed to enqueue download job %s: %s", job.id, exc)
        job.status = "failed"
        job.error_log = f"Enqueue failed: {exc}"
        await db.commit()
        return {
            "status": "error",
            "source_id": str(ss.id),
            "job_id": str(job.id),
            "error": str(exc),
        }

    await db.commit()
    return {
        "status": "enqueued",
        "source_id": str(ss.id),
        "job_id": str(job.id),
        "source_url": normalized_url,
    }
