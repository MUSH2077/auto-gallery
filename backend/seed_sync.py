"""Bootstrap the subscription sync scheduler on first start.

Only enqueues sync_subscriptions if no existing instance is in the scheduled
queue, preventing double-execution on container restarts.
"""
import redis as redis_lib
from rq import Queue
from app.config import settings

r = redis_lib.from_url(settings.redis_url)
q = Queue(name="scheduled", connection=r)

# Check if a sync_subscriptions job is already queued or scheduled
has_sync = any("sync_subscriptions" in (getattr(j, 'func_name', '') or str(j))
               for j in q.get_jobs())
if not has_sync:
    q.enqueue("app.jobs.subscription_sync.sync_subscriptions")
    print("Bootstrapped initial subscription sync")
else:
    print("Subscription sync already queued, skipping bootstrap")
