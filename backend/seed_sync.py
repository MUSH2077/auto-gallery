"""Bootstrap the subscription sync scheduler on first start.

Only schedules sync_subscriptions if no existing instance is queued, scheduled,
or started, preventing double-execution on container restarts. The first scan is
delayed by the configured scan interval so a container restart never triggers
business sync immediately.
"""
import asyncio
from datetime import timedelta

import redis as redis_lib
from rq import Queue
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from app.config import settings
from app.database import async_session
from app.services.settings import DEFAULT_SCAN_MINUTES, get_scheduler_config
from app.jobs.subscription_sync import sync_subscriptions

r = redis_lib.from_url(settings.redis_url)
q = Queue(name="scheduled", connection=r)

def _job_is_sync(job) -> bool:
    return "sync_subscriptions" in (getattr(job, "func_name", "") or str(job))


async def _scan_interval_minutes() -> int:
    try:
        async with async_session() as db:
            config = await get_scheduler_config(db)
            return max(int(config.get("scheduler_scan_interval_minutes", DEFAULT_SCAN_MINUTES)), 5)
    except Exception:
        return DEFAULT_SCAN_MINUTES


# Check if a sync_subscriptions job is already queued, scheduled, or started
queued_jobs = q.get_jobs()
scheduled_registry = ScheduledJobRegistry(queue=q)
started_registry = StartedJobRegistry(queue=q)
scheduled_jobs = [
    q.fetch_job(job_id)
    for job_id in scheduled_registry.get_job_ids()
]
started_jobs = [
    q.fetch_job(job_id)
    for job_id in started_registry.get_job_ids()
]
has_sync = any(_job_is_sync(j) for j in queued_jobs + [j for j in scheduled_jobs if j] + [j for j in started_jobs if j])
if not has_sync:
    interval = asyncio.run(_scan_interval_minutes())
    q.enqueue_in(timedelta(minutes=interval), sync_subscriptions)
    print(f"Scheduled initial subscription sync scan in {interval} minutes")
else:
    print("Subscription sync already queued, skipping bootstrap")
