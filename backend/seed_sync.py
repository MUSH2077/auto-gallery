"""Bootstrap the subscription sync scheduler on first start.

Only schedules sync_subscriptions if no existing instance is queued, scheduled,
or started, preventing double-execution on container restarts. The first scan is
delayed by the configured scan interval so a container restart never triggers
business sync immediately.
"""
import asyncio
import time

import redis as redis_lib
from app.database import async_session
from app.services.queue_admission import QueueAdmissionError
from app.services.scheduler_loop import ensure_next_subscription_scan
from app.services.settings import DEFAULT_SCAN_MINUTES, get_scheduler_config


async def _scan_interval_minutes() -> int:
    """Read scheduler scan interval from DB with retry for slow-starting PostgreSQL."""
    max_retries = 30
    retry_delay = 2  # seconds
    for attempt in range(max_retries):
        try:
            async with async_session() as db:
                config = await get_scheduler_config(db)
                return max(int(config.get("scheduler_scan_interval_minutes", DEFAULT_SCAN_MINUTES)), 5)
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"DB not ready (attempt {attempt + 1}/{max_retries}): {e}; retrying in {retry_delay}s")
                time.sleep(retry_delay)
            else:
                print(f"DB unavailable after {max_retries} attempts, using default interval ({DEFAULT_SCAN_MINUTES} min)")
    return DEFAULT_SCAN_MINUTES


# The scheduler container runs this before its worker supervisor.  Redis
# capacity/unavailability is an expected degraded state, so wait in-process
# with bounded backoff instead of entering a Docker restart loop.
retry_delays = (5, 15, 30, 60)
attempt = 0
while True:
    try:
        interval = asyncio.run(_scan_interval_minutes())
        ensured = ensure_next_subscription_scan(interval)
        if ensured.get("created"):
            print(f"Scheduled initial subscription sync scan in {interval} minutes")
        else:
            print("Subscription sync already queued, skipping bootstrap")
        break
    except QueueAdmissionError as exc:
        delay = retry_delays[min(attempt, len(retry_delays) - 1)]
        attempt += 1
        print(
            f"Redis queue admission paused scheduler bootstrap ({exc.code}); "
            f"retrying in {delay}s",
            flush=True,
        )
        time.sleep(delay)
    except (redis_lib.RedisError, OSError) as exc:
        delay = retry_delays[min(attempt, len(retry_delays) - 1)]
        attempt += 1
        print(
            f"Redis unavailable during scheduler bootstrap ({type(exc).__name__}); "
            f"retrying in {delay}s",
            flush=True,
        )
        time.sleep(delay)
