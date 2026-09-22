"""Daily Pixiv ranking schedule and durable job identity helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
from rq.job import Job

from app.remote_discovery.pixiv import PIXIV_RANKING_MODES
from app.services.queue_admission import checked_enqueue
from app.services.redis_client import get_redis


PIXIV_RANKING_TIMEZONE = ZoneInfo("Asia/Tokyo")
PIXIV_RANKING_READY_TIME = time(hour=12, minute=15)


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
        return {
            "created": False,
            "job_id": job_id,
            "ranking_date": plan.ranking_date.isoformat(),
            "status": str(getattr(status, "value", status)),
        }

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
    "ensure_pixiv_ranking_sync",
    "pixiv_ranking_job_id",
    "pixiv_ranking_plan",
]
