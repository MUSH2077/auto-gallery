"""
Worker control utilities — Redis pub/sub listeners for pause/cancel signals,
heartbeat publishing, and safe interrupt point checks.

Used by both download workers (which manage subprocesses) and import workers
(which process works in a loop).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from typing import Any

from app.services.redis_client import get_redis
from app.services.redis_pubsub import TaskChannel, TaskEventPublisher

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 10  # seconds
CANCEL_GRACE_SECONDS = 3  # wait between SIGTERM and SIGKILL


# ──────────────────────────────────────────────
# Control listener
# ──────────────────────────────────────────────

class ControlListener:
    """Listens on ``task:{job_id}:control`` for pause/cancel commands.

    Runs in a background daemon thread. When a control command is received,
    it triggers the appropriate action (SIGTERM the process group, set
    a stop flag, etc.).

    Usage (download worker — manages subprocess)::

        listener = ControlListener(job_id, proc_pid=proc.pid)
        listener.start()
        # ... run gallery-dl via Popen ...
        listener.stop()

    Usage (import worker — loop-based)::

        listener = ControlListener(job_id)  # no proc_pid
        listener.start()
        for work in works:
            if listener.should_stop():
                break
            process(work)
        listener.stop()
    """

    def __init__(self, job_id: str, *, proc_pid: int | None = None):
        self.job_id = job_id
        self.proc_pid = proc_pid
        self._stop_event = threading.Event()
        self._command: str | None = None
        self._reason: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def command(self) -> str | None:
        return self._command

    @property
    def reason(self) -> str | None:
        return self._reason

    def should_stop(self) -> bool:
        """Return True if the worker should stop (pause or cancel requested)."""
        return self._stop_event.is_set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._listen, daemon=True, name=f"ctrl-{self.job_id[:8]}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ── internal ──────────────────────────────

    def _listen(self) -> None:
        r = get_redis()
        pubsub = r.pubsub()
        channel = TaskChannel.control(self.job_id)

        try:
            pubsub.subscribe(channel)
            logger.debug("Control listener started for job %s", self.job_id)

            for message in pubsub.listen():
                if self._stop_event.is_set():
                    break
                if message["type"] != "message":
                    continue

                try:
                    data = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                cmd = data.get("command", "")
                reason = data.get("reason")

                if cmd == "pause":
                    logger.info("Pause command received for job %s", self.job_id)
                    self._command = "pause"
                    self._reason = reason
                    self._handle_pause()
                    self._stop_event.set()
                    break

                elif cmd == "cancel":
                    logger.info("Cancel command received for job %s", self.job_id)
                    self._command = "cancel"
                    self._reason = reason
                    self._handle_cancel()
                    self._stop_event.set()
                    break

        finally:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception:
                pass

    def _handle_pause(self) -> None:
        if self.proc_pid is not None:
            self._kill_process_group(signal.SIGTERM)

    def _handle_cancel(self) -> None:
        if self.proc_pid is not None:
            self._kill_process_group(signal.SIGTERM, grace=CANCEL_GRACE_SECONDS)

    def _kill_process_group(self, sig: int, grace: int = 0) -> None:
        try:
            pgid = os.getpgid(self.proc_pid)
            os.killpg(pgid, sig)
            logger.info("Sent signal %d to process group %d (job %s)", sig, pgid, self.job_id)
        except (ProcessLookupError, OSError):
            return

        if grace > 0:
            time.sleep(grace)
            try:
                os.killpg(pgid, signal.SIGKILL)
                logger.info("Sent SIGKILL to process group %d (job %s)", pgid, self.job_id)
            except (ProcessLookupError, OSError):
                pass


# ──────────────────────────────────────────────
# Heartbeat publisher
# ──────────────────────────────────────────────

class HeartbeatPublisher:
    """Publishes heartbeat pings to ``task:{job_id}:heartbeat`` every 10s.

    Usage::

        hb = HeartbeatPublisher(job_id, task_type="download", pid=proc.pid)
        hb.start()
        try:
            do_work()
        finally:
            hb.stop()
    """

    def __init__(self, job_id: str, task_type: str, *, pid: int | None = None):
        self.job_id = job_id
        self.task_type = task_type
        self.pid = pid or os.getpid()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"hb-{self.job_id[:8]}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                TaskEventPublisher.publish_heartbeat(
                    self.job_id, self.task_type, pid=self.pid,
                )
            except Exception:
                logger.debug("Heartbeat failed for job %s", self.job_id, exc_info=True)


# ──────────────────────────────────────────────
# Control signal check (poll-based, for import workers)
# ──────────────────────────────────────────────

def check_control_signal(job_id: str) -> dict[str, Any] | None:
    """Non-blocking check for a control signal.

    The publisher (TaskEventPublisher.send_control) also sets a Redis key
    with a short TTL as a side channel. Import workers poll this key between
    iterations instead of running a full pubsub listener thread.

    Returns ``{"command": "...", "reason": "..."}`` or ``None``.
    """
    try:
        r = get_redis()
        signal_key = f"task:{job_id}:signal"
        raw = r.get(signal_key)
        if raw:
            r.delete(signal_key)
            return json.loads(raw)
    except Exception:
        pass
    return None
