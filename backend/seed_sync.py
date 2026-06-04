"""Bootstrap the subscription sync scheduler on first start.

Only enqueues sync_subscriptions if no existing instance is in the scheduled
queue, preventing double-execution on container restarts.
"""
import redis as redis_lib
from rq import Queue
from rq.registry import ScheduledJobRegistry
from app.config import settings

r = redis_lib.from_url(settings.redis_url)
q = Queue(name="scheduled", connection=r)

def _job_is_sync(job) -> bool:
    return "sync_subscriptions" in (getattr(job, "func_name", "") or str(job))


# Check if a sync_subscriptions job is already queued or scheduled
queued_jobs = q.get_jobs()
scheduled_registry = ScheduledJobRegistry(queue=q)
scheduled_jobs = [
    q.fetch_job(job_id)
    for job_id in scheduled_registry.get_job_ids()
]
has_sync = any(_job_is_sync(j) for j in queued_jobs + [j for j in scheduled_jobs if j])
if not has_sync:
    q.enqueue("app.jobs.subscription_sync.sync_subscriptions")
    print("Bootstrapped initial subscription sync")
else:
    print("Subscription sync already queued, skipping bootstrap")
