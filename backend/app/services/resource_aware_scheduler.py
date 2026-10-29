"""RQ scheduler that wakes resource-aware workers after scheduled promotion."""

from __future__ import annotations

import json
import logging
import os
import time

from redis.exceptions import RedisError

from rq.scheduler import RQScheduler
from rq.utils import current_timestamp

from app.services.queue_admission import notify_queue_worker

logger = logging.getLogger(__name__)


class ResourceAwareScheduler(RQScheduler):
    """Preserve RQ promotion while hinting workers about newly-ready queues."""

    def start(self):
        if os.environ.get("WORKER_SHARED_INITIALIZATION", "0").strip().lower() in {"1", "true"}:
            self._shared_consumer_pid = os.getpid()
        return super().start()

    def _install_signal_handlers(self):
        super()._install_signal_handlers()
        consumer_pid = getattr(self, "_shared_consumer_pid", None)
        if consumer_pid is not None:
            from app.services.shared_worker_process import follow_consumer_lifetime

            # Install RQ's normal stop handler first, so parent loss releases
            # scheduler locks and does not leave multiprocessing pipes open.
            follow_consumer_lifetime(consumer_pid)

    def _queues_with_due_jobs(self) -> set[str]:
        """Return queues with due jobs without changing their registries."""

        try:
            # RQ prepares registries lazily inside enqueue_scheduled_jobs().
            # Prepare them here too so the first scheduler pass can identify
            # its queues without duplicating RQ's promotion algorithm.
            if not self._scheduled_job_registries and self._acquired_locks:
                self.prepare_registries()

            timestamp = current_timestamp()
            return {
                str(registry.name)
                for registry in self._scheduled_job_registries
                if registry.get_jobs_to_schedule(timestamp)
            }
        except Exception:
            # Promotion is the source of truth.  A Redis read used only for a
            # responsiveness hint must never block the scheduler.
            logger.debug(
                "Unable to identify due queues before scheduled promotion",
                exc_info=True,
            )
            return set()

    def enqueue_scheduled_jobs(self):
        """Promote through RQ, then best-effort wake each due resource lane."""

        try:
            if not self._scheduled_job_registries and self._acquired_locks:
                self.prepare_registries()
            download_registries = [
                registry for registry in self._scheduled_job_registries
                if registry.name in self._acquired_locks
                and (registry.name == "downloads" or registry.name.startswith("downloads:"))
            ]
            if download_registries:
                from app.services.download_resource_wait import promote_resource_waits

                promote_resource_waits(self.connection, download_registries)
        except Exception:
            # This is only an acceleration path; RQ still owns due promotion.
            logger.debug("Download resource wake check unavailable", exc_info=True)
        due_queue_names = self._queues_with_due_jobs()
        result = super().enqueue_scheduled_jobs()

        for queue_name in due_queue_names:
            notify_queue_worker(queue_name, self.connection)

        return result

    def work(self):
        if not any(name == "downloads" or name.startswith("downloads:") for name in self._queue_names):
            return super().work()

        from app.services.resource_pressure import RESOURCE_CONTROL_CHANNEL

        self._install_signal_handlers()
        self.register_birth()
        pubsub = None
        try:
            while not self._stop_requested:
                if self.should_reacquire_locks:
                    self.acquire_locks()
                self.enqueue_scheduled_jobs()
                self.heartbeat()
                # Subscribe in the scheduler child. The original RQ interval
                # and Retry deadline remain the fallback if notifications fail.
                try:
                    if pubsub is None:
                        pubsub = self.connection.pubsub(ignore_subscribe_messages=True)
                        pubsub.subscribe(RESOURCE_CONTROL_CHANNEL)
                    deadline = time.monotonic() + self.interval
                    while not self._stop_requested and time.monotonic() < deadline:
                        message = pubsub.get_message(timeout=min(.5, max(0, deadline - time.monotonic())))
                        if message is None:
                            continue
                        try:
                            event = json.loads(message["data"])
                        except (ValueError, TypeError):
                            continue
                        if event.get("type") in {"resource_released", "download_resource_waiting"}:
                            # Coalesce bursts from partial lease cleanup. The
                            # admission probe still rejects occupied archives.
                            time.sleep(.05)
                            break
                except RedisError:
                    logger.debug("Download scheduler notification unavailable", exc_info=True)
                    if pubsub is not None:
                        pubsub.close()
                        pubsub = None
                    time.sleep(self.interval)
            self.stop()
        finally:
            if pubsub is not None:
                pubsub.close()
