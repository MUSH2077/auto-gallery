import logging
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import select, func, update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services.job_manifest import append_manifest_event, update_manifest
from app.models.task_state import DOWNLOAD_RUNNING_STATUSES
from app.services.job_progress import apply_download_progress
from app.services.locks import redis_lock
from app.services.redis_client import get_redis
from app.services.settings import get_download_defaults, get_scheduler_config
from app.services.search_projection_outbox import request_search_projection

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200
RUNNING_STATUSES = DOWNLOAD_RUNNING_STATUSES


def skip_result(source_id: UUID | str, code: str, message: str | None = None, **details) -> dict:
    reason = {
        "code": code,
        "message": message or code.replace("_", " "),
        "retryable": code in {
            "lock_busy",
            "already_running",
            "recent_failure_backoff",
            "resource_pressure",
            "queue_saturated",
            "enqueue_busy",
            "redis_capacity",
            "redis_unwritable",
            "disk_backpressure",
            "import_backpressure",
            "storage_unavailable",
            "admission_check_failed",
        },
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
    sub = await db.get(Subscription, ss.subscription_id)
    # A success changes the interval base.  NULL invalidates the old due time;
    # the next fair coverage pass recomputes interval/fixed/manual semantics in
    # its short claim transaction.
    ss.next_sync_at = None
    await request_search_projection(
        db,
        creator_ids=[sub.creator_id] if sub else (),
        repository_ids=[ss.id],
        subscription_ids=[ss.subscription_id],
    )


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
    parent_task_id: UUID | None = None,
    force_reason: str | None = None,
    scheduler_config: dict | None = None,
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

    # ``force`` may bypass schedule-frequency checks, never hard capacity.
    # Automatic scans stop producing under host pressure; manual requests may
    # remain in the bounded queue and will wait at the worker heavy-I/O gate.
    try:
        from app.services.backpressure import download_backpressure_reason

        pressure = await download_backpressure_reason(
            db,
            automatic=trigger == "scheduler",
            include_queue=True,
        )
        if pressure:
            code = str(pressure.get("code") or "download_unavailable")
            details = {key: value for key, value in pressure.items() if key != "code"}
            return skip_result(ss.id, code, **details)
    except Exception as exc:
        logger.warning("Unable to evaluate download admission", exc_info=True)
        return skip_result(
            ss.id,
            "admission_check_failed",
            message="Download admission checks are unavailable",
            error_type=type(exc).__name__,
        )

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
            force_reason=force_reason,
            had_sync_baseline=bool(ss.last_synced_at),
            subscription_source_id=str(ss.id),
            subscription_id=str(sub.id),
            source=ss.source,
            source_url=normalized_url,
        )
        append_manifest_event(job, "created", trigger=trigger)
        if scheduler_config is None:
            scheduler_config = await get_scheduler_config(db)
        from app.jobs.subscription_sync import next_subscription_check_at

        next_sync_at = next_subscription_check_at(
            sub,
            scheduler_config,
            ss.last_synced_at,
            now,
            now,
        )
        # Flush caller-owned state before opening the SAVEPOINT. SQLAlchemy
        # flushes pending state when begin_nested() starts; doing it explicitly
        # keeps unrelated outer-transaction errors out of the candidate-job
        # IntegrityError handler below.
        await db.flush()
        try:
            # A source-level unique/running race must not roll back the caller's
            # parent batch TaskRun or progress updates.  Add/flush inside a
            # SAVEPOINT so only this candidate DownloadJob is discarded.
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:
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

        await request_search_projection(
            db,
            subscription_ids=[sub.id],
        )

        from app.services.download_dispatch import prepare_download_dispatch, publish_prepared_download

        queue_name = f"downloads:{job.source}"
        prepared = await prepare_download_dispatch(
            db,
            job,
            queue_name=queue_name,
            parent_task_id=parent_task_id,
            job_timeout=RQ_JOB_TIMEOUT,
            action=trigger,
        )
        try:
            await publish_prepared_download(
                db,
                job,
                prepared,
                job_timeout=RQ_JOB_TIMEOUT,
                action=trigger,
            )
        except Exception as exc:
            from app.services.backpressure import DownloadAdmissionError

            logger.error("Failed to enqueue download job %s: %s", job.id, exc)
            code = exc.code if isinstance(exc, DownloadAdmissionError) else "enqueue_failed"
            details = dict(exc.details) if isinstance(exc, DownloadAdmissionError) else {}
            return {
                "status": "error",
                "source_id": str(ss.id),
                "job_id": str(job.id),
                "error": str(exc),
                "skip_reason": code,
                "reason": {"code": code, "message": str(exc), "retryable": True, "details": details},
            }

        # Publication is the scheduling boundary. A rejected Redis enqueue must
        # leave the logical slot due; only an accepted deterministic RQ job may
        # advance the source to its next interval/fixed-time slot.
        await db.execute(
            sql_update(SubscriptionSource)
            .where(SubscriptionSource.id == ss.id)
            .values(last_attempted_at=now, next_sync_at=next_sync_at)
        )
        await db.commit()

        try:
            from app.services.job_progress import publish_progress

            publish_progress(str(job.id), "download", job.progress_data)
        except Exception:
            logger.warning("Failed to publish queued progress for %s", job.id, exc_info=True)
        return {
            "status": "enqueued",
            "source_id": str(ss.id),
            "job_id": str(job.id),
            "task_id": str(prepared.task.id),
            "source_url": normalized_url,
        }
