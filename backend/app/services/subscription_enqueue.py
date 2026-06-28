import logging
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_state import DOWNLOAD_RUNNING_STATUSES, transition_download_job
from app.services.job_progress import apply_download_progress
from app.services.locks import redis_lock
from app.services.redis_client import get_redis
from app.services.settings import get_download_defaults

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200
RUNNING_STATUSES = DOWNLOAD_RUNNING_STATUSES


def skip_result(source_id: UUID | str, code: str, message: str | None = None, **details) -> dict:
    reason = {
        "code": code,
        "message": message or code.replace("_", " "),
        "retryable": code in {"lock_busy", "already_running", "recent_failure_backoff"},
        "details": details or {},
    }
    result = {
        "status": "skipped",
        "source_id": str(source_id),
        "skip_reason": code,
        "reason": reason,
    }
    result.update(details)
    return result


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
        return skip_result(subscription_source_id, "source_not_found")

    sub = await db.get(Subscription, ss.subscription_id)
    if not sub:
        return skip_result(ss.id, "subscription_not_found")
    if not force and not ss.is_enabled:
        return skip_result(ss.id, "source_disabled")
    if not force and ss.auth_healthy is False:
        return skip_result(ss.id, "auth_unhealthy", auth_status=ss.auth_status, auth_error_reason=ss.auth_error_reason)
    if not force:
        try:
            r_ph = get_redis()
            ph = r_ph.hgetall(f"proxy:health:{ss.source}")
            if ph and ph.get(b"status", b"").decode() == "degraded":
                return skip_result(ss.id, "proxy_degraded", warnings=ph.get(b"warnings", b"").decode())
        except Exception:
            pass
    if not ss.source_url:
        return skip_result(ss.id, "source_url_empty")

    try:
        provider = registry.get(ss.source)
    except KeyError:
        return skip_result(ss.id, "unknown_provider", source=ss.source)
    if not provider.capabilities.can_download:
        return skip_result(ss.id, "provider_not_downloadable", source=ss.source)

    if not force:
        try:
            from app.services.backpressure import download_backpressure_reason
            pressure = await download_backpressure_reason(db)
            if pressure:
                return skip_result(ss.id, pressure.pop("code"), **pressure)
        except Exception:
            # During a rolling migration the ledger table may not exist yet;
            # availability checks must not prevent the migration from settling.
            logger.warning("Unable to evaluate download backpressure", exc_info=True)

    normalized_url = provider.normalize_url(ss.source_url) or ss.source_url
    if not provider.validate_url(normalized_url):
        return skip_result(ss.id, "url_invalid", source_url=ss.source_url)
    if normalized_url != ss.source_url:
        ss.source_url = normalized_url

    lock_key = f"lock:subscription-source-sync:{ss.id}"
    async with redis_lock(lock_key, ttl_seconds=120) as acquired:
        if not acquired:
            return skip_result(ss.id, "lock_busy")

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
            return skip_result(ss.id, "already_running", job_id=str(running_job.id))

        if trigger == "scheduler" and not force:
            latest = await _latest_job_for_source(db, ss.id)
            latest_created_at = _as_utc(latest.created_at) if latest else None
            if latest and latest_created_at and latest.status in ("failed", "stale"):
                defaults = await get_download_defaults(db)
                backoff = int(defaults.get("retry_backoff_base_seconds", 60))
                max_retries = int(defaults.get("max_retries", 3))
                backoff_until = latest_created_at + timedelta(seconds=max(backoff, 1) * max(max_retries, 1))
                if now < backoff_until:
                    return skip_result(ss.id, "recent_failure_backoff", latest_job_id=str(latest.id), backoff_until=backoff_until.isoformat())

        job = DownloadJob(
            subscription_id=sub.id,
            subscription_source_id=ss.id,
            source=ss.source,
            source_url=normalized_url,
            status="enqueued",
        )
        apply_download_progress(
            job,
            "enqueued",
            "Queued; waiting for download worker",
            publish=False,
        )
        update_manifest(job,
            trigger=trigger,
            subscription_source_id=str(ss.id),
            subscription_id=str(sub.id),
            source=ss.source,
            source_url=normalized_url,
        )
        append_manifest_event(job, "created", trigger=trigger)
        ss.last_attempted_at = now
        db.add(job)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            running = await db.execute(
                select(DownloadJob).where(
                    DownloadJob.subscription_source_id == ss.id,
                    DownloadJob.status.in_(RUNNING_STATUSES),
                ).order_by(DownloadJob.created_at.desc()).limit(1)
            )
            running_job = running.scalar_one_or_none()
            if running_job:
                return skip_result(ss.id, "already_running", job_id=str(running_job.id))
            raise

        try:
            from rq import Queue
            from app.services.tasks import TaskService
            await TaskService(db).ensure_download_task(job)

            queue_name = f"downloads:{job.source}"
            Queue(name=queue_name, connection=get_redis()).enqueue(
                "app.jobs.download.run_download_job",
                str(job.id),
                job_timeout=RQ_JOB_TIMEOUT,
            )
            apply_download_progress(
                job,
                "enqueued",
                "Queued; waiting for download worker",
            )
            append_manifest_event(job, "enqueued", queue="downloads")
        except Exception as exc:
            logger.error("Failed to enqueue download job %s: %s", job.id, exc)
            transition_download_job(job, "failed", f"Enqueue failed: {exc}")
            append_manifest_event(job, "enqueue_failed", error=str(exc))
            await db.commit()
            return {
                "status": "error",
                "source_id": str(ss.id),
                "job_id": str(job.id),
                "error": str(exc),
                "reason": {"code": "enqueue_failed", "message": str(exc), "retryable": True, "details": {}},
            }

        await db.commit()
        return {
            "status": "enqueued",
            "source_id": str(ss.id),
            "job_id": str(job.id),
            "source_url": normalized_url,
        }
