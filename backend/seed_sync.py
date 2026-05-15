import redis as redis_lib
from rq import Queue
from app.config import settings

r = redis_lib.from_url(settings.redis_url)
q = Queue(name="scheduled", connection=r)
q.enqueue("app.jobs.subscription_sync.sync_subscriptions")
print("Bootstrapped initial subscription sync")
