"""Daily Pixiv ranking schedule and durable job identity helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.source_ranking_snapshot import SourceRankingSnapshot
from app.remote_discovery.pixiv import PIXIV_RANKING_MODES
from app.services.queue_admission import checked_enqueue, checked_enqueue_in
from app.services.redis_client import get_redis
from app.services.work_heat import OFFICIAL_RANK_MAX_AGE


PIXIV_RANKING_TIMEZONE = ZoneInfo("Asia/Tokyo")
PIXIV_RANKING_READY_TIME = time(hour=12, minute=15)
PIXIV_HEAT_EXPIRY_GRACE = timedelta(seconds=1)
_SATISFIED_JOB_STATUSES = frozenset(
    {"queued", "scheduled", "deferred", "started", "busy", "finished"}
)


@dataclass(frozen=True, slots=True)
class PixivRankingPlan:
    ranking_date: date
    ready_at: datetime


def pixiv_ranking_plan(now: datetime | None = None) -> PixivRankingPlan:
    """Return the newest ranking date expected to be published by ``now``."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Pixiv ranking schedule requires a timezone-aware datetime")
    local_now = current.astimezone(PIXIV_RANKING_TIMEZONE)
    local_ready = datetime.combine(
        local_now.date(),
        PIXIV_RANKING_READY_TIME,
        tzinfo=PIXIV_RANKING_TIMEZONE,
    )
    if local_now < local_ready:
        local_ready -= timedelta(days=1)
    return PixivRankingPlan(
        ranking_date=local_ready.date() - timedelta(days=1),
        ready_at=local_ready.astimezone(UTC),
    )


def pixiv_ranking_job_id(ranking_date: date) -> str:
    return f"pixiv-ranking-{ranking_date.isoformat()}"


def pixiv_heat_expiry_job_id(expires_at: datetime) -> str:
    if expires_at.tzinfo is None:
        raise ValueError("Pixiv heat expiry requires a timezone-aware datetime")
    return f"pixiv-heat-expiry-{int(expires_at.timestamp())}"


def schedule_pixiv_heat_expiry(
    fetched_at: datetime,
    *,
    now: datetime | None = None,
    redis_client=None,
) -> dict:
    """Schedule a guarded expiry check for one successful ranking snapshot."""

    if fetched_at.tzinfo is None:
        raise ValueError("Pixiv ranking fetched_at must be timezone-aware")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Pixiv heat expiry schedule requires a timezone-aware datetime")
    expires_at = fetched_at + OFFICIAL_RANK_MAX_AGE + PIXIV_HEAT_EXPIRY_GRACE
    job_id = pixiv_heat_expiry_job_id(expires_at)
    redis_client = redis_client or get_redis()
    try:
        existing = Job.fetch(job_id, connection=redis_client)
    except NoSuchJobError:
        existing = None
    if existing is not None:
        status = existing.get_status(refresh=True)
        status_text = str(getattr(status, "value", status)).casefold()
        if status_text in _SATISFIED_JOB_STATUSES:
            return {
                "created": False,
                "job_id": job_id,
                "expires_at": expires_at.isoformat(),
                "status": status_text,
            }
        # Failed, canceled, or stopped checks are not durable evidence that the
        # snapshot was invalidated. Reuse the deterministic id after removing
        # the terminal record; a finished check remains satisfied.
        existing.delete()

    from app.jobs.work_heat import expire_pixiv_heat

    queue = Queue(name="scheduled", connection=redis_client)
    checked_enqueue_in(
        queue,
        max(timedelta(), expires_at - current),
        expire_pixiv_heat,
        job_id=job_id,
        job_timeout=5 * 60,
        result_ttl=48 * 60 * 60,
        failure_ttl=48 * 60 * 60,
        retry=Retry(max=3, interval=[60, 300, 900]),
    )
    return {
        "created": True,
        "job_id": job_id,
        "expires_at": expires_at.isoformat(),
        "status": "scheduled",
    }


async def _latest_pixiv_ranking_fetched_at() -> datetime | None:
    try:
        async with async_session() as db:
            return (
                await db.execute(
                    select(func.max(SourceRankingSnapshot.fetched_at)).where(
                        SourceRankingSnapshot.source == "pixiv"
                    )
                )
            ).scalar_one_or_none()
    finally:
        # The long-lived supervisor calls this through a fresh asyncio.run loop
        # every minute. Do not retain asyncpg connections bound to a closed loop.
        await engine.dispose()


def ensure_latest_pixiv_heat_expiry(*, redis_client=None) -> dict:
    """Reconcile the latest persisted snapshot with its durable expiry job."""

    latest_fetched_at = asyncio.run(_latest_pixiv_ranking_fetched_at())
    if latest_fetched_at is None:
        return {"created": False, "status": "skipped", "reason": "no_ranking_snapshot"}
    return schedule_pixiv_heat_expiry(
        latest_fetched_at,
        redis_client=redis_client or get_redis(),
    )


def ensure_pixiv_ranking_sync(
    *,
    now: datetime | None = None,
    redis_client=None,
) -> dict:
    """Ensure the newest expected Pixiv ranking date has one durable RQ job."""

    redis_client = redis_client or get_redis()
    plan = pixiv_ranking_plan(now)
    job_id = pixiv_ranking_job_id(plan.ranking_date)
    try:
        existing = Job.fetch(job_id, connection=redis_client)
    except NoSuchJobError:
        existing = None
    if existing is not None:
        status = existing.get_status(refresh=True)
        status_text = str(getattr(status, "value", status)).casefold()
        if status_text in _SATISFIED_JOB_STATUSES:
            return {
                "created": False,
                "job_id": job_id,
                "ranking_date": plan.ranking_date.isoformat(),
                "status": status_text,
            }
        # A terminal failure does not fulfill the daily sync. Remove its RQ
        # record so the deterministic job id can be queued again.
        existing.delete()

    from app.jobs.pixiv_ranking_sync import sync_pixiv_rankings

    queue = Queue(name="scheduled", connection=redis_client)
    checked_enqueue(
        queue,
        sync_pixiv_rankings,
        plan.ranking_date.isoformat(),
        job_id=job_id,
        job_timeout=3600,
        result_ttl=48 * 60 * 60,
        failure_ttl=48 * 60 * 60,
        retry=Retry(max=3, interval=[5 * 60, 15 * 60, 30 * 60]),
    )
    return {
        "created": True,
        "job_id": job_id,
        "ranking_date": plan.ranking_date.isoformat(),
        "status": "queued",
    }


__all__ = [
    "PIXIV_RANKING_MODES",
    "PixivRankingPlan",
    "ensure_latest_pixiv_heat_expiry",
    "ensure_pixiv_ranking_sync",
    "pixiv_heat_expiry_job_id",
    "pixiv_ranking_job_id",
    "pixiv_ranking_plan",
    "schedule_pixiv_heat_expiry",
]
