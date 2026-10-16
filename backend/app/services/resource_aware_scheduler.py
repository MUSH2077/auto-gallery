"""RQ scheduler that wakes resource-aware workers after scheduled promotion."""

from __future__ import annotations

import logging
import os

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

        due_queue_names = self._queues_with_due_jobs()
        result = super().enqueue_scheduled_jobs()

        for queue_name in due_queue_names:
            notify_queue_worker(queue_name, self.connection)

        return result
