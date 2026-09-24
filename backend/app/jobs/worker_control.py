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
from typing import Any, Callable

from app.services.redis_client import get_redis
from app.services.redis_pubsub import TaskChannel, TaskEventPublisher

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 10  # seconds


def signal_process_group(proc_pid: int, sig: int) -> bool:
    """Signal a subprocess group without waiting or guessing that it exited.

    The thread receiving Redis control messages must stay non-blocking.  The
    owner of the ``Popen`` object is responsible for ``wait()`` and escalation,
    because it is the only component that can reap the process conclusively.
    """

    try:
        pgid = os.getpgid(proc_pid)
    except ProcessLookupError:
        # Once the leader is gone, its numeric pid may already belong to an
        # unrelated process.  Subprocess owners that must clean up surviving
        # helpers retain the original pgid and handle that explicitly.
        return False
    except OSError:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, OSError):
        return False


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
        self._process_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._command: str | None = None
        self._reason: str | None = None
        self._thread: threading.Thread | None = None
        self._pubsub_lock = threading.Lock()
        self._pubsub: Any | None = None

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
        # ``PubSub.listen()`` blocks while no command is arriving.  Closing the
        # subscriber is what releases that socket back to the shared bounded
        # Redis pool; setting the event alone can strand one connection per
        # completed job indefinitely.
        with self._pubsub_lock:
            pubsub = self._pubsub
            self._pubsub = None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                logger.debug(
                    "Unable to close control subscriber for job %s",
                    self.job_id,
                    exc_info=True,
                )
        thread = self._thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=1)
            if thread.is_alive():
                logger.warning(
                    "Control listener for job %s did not stop within one second",
                    self.job_id,
                )

    def detach_process(self, expected_pid: int) -> bool:
        """Stop signalling one completed child, fenced by its known pid.

        Waiting for an in-flight signal while holding this lock establishes the
        handoff boundary: after this method returns successfully no listener
        callback can still read or signal the detached pid.
        """

        with self._process_lock:
            if self.proc_pid != expected_pid:
                return False
            self.proc_pid = None
            return True

    # ── internal ──────────────────────────────

    def _listen(self) -> None:
        r = get_redis()
        pubsub = r.pubsub()
        channel = TaskChannel.control(self.job_id)

        try:
            with self._pubsub_lock:
                self._pubsub = pubsub
                stopped_before_subscribe = self._stop_event.is_set()
            if stopped_before_subscribe:
                return
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

        except Exception:
            if not self._stop_event.is_set():
                logger.warning(
                    "Control listener failed for job %s",
                    self.job_id,
                    exc_info=True,
                )
        finally:
            with self._pubsub_lock:
                owns_pubsub = self._pubsub is pubsub
                if owns_pubsub:
                    self._pubsub = None
            if owns_pubsub:
                try:
                    pubsub.unsubscribe(channel)
                    pubsub.close()
                except Exception:
                    pass

    def _handle_pause(self) -> None:
        self._kill_process_group(signal.SIGTERM)

    def _handle_cancel(self) -> None:
        self._kill_process_group(signal.SIGTERM)

    def _kill_process_group(self, sig: int) -> None:
        with self._process_lock:
            proc_pid = self.proc_pid
            if proc_pid is None:
                return
            if signal_process_group(proc_pid, sig):
                logger.info(
                    "Sent signal %d to process group for pid %d (job %s)",
                    sig,
                    proc_pid,
                    self.job_id,
                )


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

    def __init__(
        self,
        job_id: str,
        task_type: str,
        *,
        pid: int | None = None,
        heartbeat_callback: Callable[[], bool] | None = None,
    ):
        self.job_id = job_id
        self.task_type = task_type
        self.pid = pid or os.getpid()
        self.heartbeat_callback = heartbeat_callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._publish_lock = threading.Lock()

    def start(self) -> None:
        if not self._publish_once():
            self._stop_event.set()
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"hb-{self.job_id[:8]}"
        )
        self._thread.start()

    def transfer_to_pid(self, pid: int) -> bool:
        """Move liveness reporting to ``pid`` and publish the handoff now."""

        with self._publish_lock:
            self.pid = pid
            return self._publish_once_locked()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop_event.wait(HEARTBEAT_INTERVAL):
            try:
                published = self._publish_once()
                if published is False:
                    self._stop_event.set()
                    return
            except Exception:
                logger.debug("Heartbeat failed for job %s", self.job_id, exc_info=True)
                if self.heartbeat_callback is not None:
                    self._stop_event.set()
                    return

    def _publish_once(self) -> bool:
        with self._publish_lock:
            return self._publish_once_locked()

    def _publish_once_locked(self) -> bool:
        try:
            return (
                self.heartbeat_callback()
                if self.heartbeat_callback is not None
                else TaskEventPublisher.publish_heartbeat(
                    self.job_id,
                    self.task_type,
                    pid=self.pid,
                )
            )
        except Exception:
            logger.debug("Heartbeat failed for job %s", self.job_id, exc_info=True)
            # Ordinary task heartbeats retry after transient Redis failures.
            # A fenced callback returning/raising cannot prove ownership and
            # must stop instead.
            return self.heartbeat_callback is None


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
