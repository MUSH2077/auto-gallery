"""Spawn supervised, resource-aware RQ worker subprocesses.

Usage:
    python worker_entrypoint.py <queue_name[,queue_name...]> <concurrency>
        [--with-scheduler]
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
RESOURCE_AWARE_WORKER_CLASS = "app.services.resource_aware_worker.ResourceAwareWorker"
RESTART_BACKOFF_SECONDS = (5, 15, 30, 60)
RESTART_WINDOW_SECONDS = 10 * 60
RESTART_CIRCUIT_SECONDS = 15 * 60
STABLE_RESET_SECONDS = 10 * 60
SUPERVISOR_STATUS_TTL_SECONDS = 75
SUPERVISOR_STATUS_INTERVAL_SECONDS = 5
SUPERVISOR_STABLE_HEARTBEAT_SECONDS = 30
WORKER_SUPERVISOR_HASH_KEY = "worker:supervisors:v1"
SCHEDULER_WATCHDOG_INTERVAL_SECONDS = 60


def resolve_concurrency(db_value, argv_value, default=3, lo=1, hi=5):
    """Pick DB -> CLI -> default and clamp the configured value."""

    for candidate in (db_value, argv_value, default):
        if candidate is None:
            continue
        try:
            n = int(candidate)
        except (TypeError, ValueError):
            continue
        return max(lo, min(hi, n))
    return default


def resolve_concurrency_cap(value=None, *, lo=1, hi=5) -> int:
    candidate = value if value is not None else os.environ.get("DOWNLOAD_CONCURRENCY_CAP", "1")
    try:
        return max(lo, min(hi, int(candidate)))
    except (TypeError, ValueError):
        return 1


def resolve_download_concurrency(db_value, argv_value) -> tuple[int, int, int]:
    configured = resolve_concurrency(db_value, argv_value, default=3)
    cap = resolve_concurrency_cap()
    return configured, cap, min(configured, cap)


def _db_download_concurrency():
    """Best-effort read from DB; boot remains possible while DB is recovering."""

    try:
        import asyncio

        from app.database import async_session, engine
        from app.services.settings import get_download_defaults

        async def _query():
            try:
                async with async_session() as db:
                    defaults = await get_download_defaults(db)
                    return defaults.get("download_concurrency")
            finally:
                # The supervisor never queries PostgreSQL again; do not retain
                # one of the 40 budgeted connections for its whole lifetime.
                await engine.dispose()

        return asyncio.run(_query())
    except Exception as exc:  # noqa: BLE001 - boot-time best effort
        print(
            f"[worker_entrypoint] DB concurrency read failed ({exc}); using fallback",
            file=sys.stderr,
        )
        return None


class RestartCircuit:
    """Bounded backoff with a crash-window circuit breaker."""

    def __init__(self) -> None:
        self.crashes: deque[float] = deque()
        self.circuit_until: float | None = None
        self.stable_since: float | None = None

    def _prune(self, now: float) -> None:
        cutoff = now - RESTART_WINDOW_SECONDS
        while self.crashes and self.crashes[0] < cutoff:
            self.crashes.popleft()

    def refresh(self, now: float) -> None:
        if self.circuit_until is not None and now >= self.circuit_until:
            self.circuit_until = None
            self.crashes.clear()
            self.stable_since = None
        self._prune(now)

    def record_exit(self, now: float) -> float:
        self.refresh(now)
        self.stable_since = None
        self.crashes.append(now)
        if len(self.crashes) >= 5:
            if self.circuit_until is None:
                self.circuit_until = now + RESTART_CIRCUIT_SECONDS
            return max(0.0, self.circuit_until - now)
        return float(RESTART_BACKOFF_SECONDS[min(len(self.crashes) - 1, len(RESTART_BACKOFF_SECONDS) - 1)])

    def is_open(self, now: float) -> bool:
        self.refresh(now)
        return self.circuit_until is not None

    def observe_fully_running(self, now: float) -> None:
        self.refresh(now)
        if self.circuit_until is not None:
            self.stable_since = None
            return
        if self.stable_since is None:
            self.stable_since = now
        elif now - self.stable_since >= STABLE_RESET_SECONDS:
            self.crashes.clear()


@dataclass
class ManagedProcess:
    process: subprocess.Popen
    queues: tuple[str, ...]
    with_scheduler: bool = False


@dataclass
class PendingRestart:
    due_at: float
    queues: tuple[str, ...]
    with_scheduler: bool = False


def _worker_command(queues: list[str], *, with_scheduler: bool) -> list[str]:
    command = [
        "rq",
        "worker",
        "-w",
        RESOURCE_AWARE_WORKER_CLASS,
        "--url",
        REDIS_URL,
    ]
    if with_scheduler:
        command.append("--with-scheduler")
    command.extend(queues)
    return command


_status_client = None
_last_status_signature = None
_last_status_publish_at = 0.0


def _supervisor_status_field(queues: list[str]) -> str:
    # Queue identity is stable across container recreation, unlike hostname.
    return "+".join(queues).replace(":", "_")


def _publish_supervisor_status(queues: list[str], payload: dict) -> None:
    global _status_client, _last_status_signature, _last_status_publish_at
    try:
        if _status_client is None:
            import redis

            _status_client = redis.from_url(
                REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        stable_value = {
            **payload,
            "queues": queues,
            "hostname": socket.gethostname(),
            "supervisor_pid": os.getpid(),
        }
        signature = json.dumps(stable_value, sort_keys=True, separators=(",", ":"))
        now = time.monotonic()
        if (
            signature == _last_status_signature
            and now - _last_status_publish_at < SUPERVISOR_STABLE_HEARTBEAT_SECONDS
        ):
            return
        value = {
            **stable_value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "valid_until": (
                datetime.now(timezone.utc)
                + timedelta(seconds=SUPERVISOR_STATUS_TTL_SECONDS)
            ).isoformat(),
        }
        _status_client.hset(
            WORKER_SUPERVISOR_HASH_KEY,
            _supervisor_status_field(queues),
            json.dumps(value, separators=(",", ":")),
        )
        _last_status_signature = signature
        _last_status_publish_at = now
    except Exception as exc:  # noqa: BLE001 - observability must not kill workers
        print(f"[worker_entrypoint] status publish failed: {exc}", file=sys.stderr)


def _register_resource_state_bridge() -> None:
    try:
        from app.services.heavy_io import register_resource_state_callback
        from app.services.tasks import update_task_resource_state

        register_resource_state_callback(update_task_resource_state)
    except Exception as exc:
        # RQ metadata is still populated if an older database is being rolled
        # forward.  ResourceAwareWorker repeats registration in the actual RQ
        # interpreter so its forked workhorse inherits the callback.
        print(
            f"[worker_entrypoint] TaskRun resource-state bridge unavailable: {exc}",
            file=sys.stderr,
        )


def build_worker_specs(
    queues: list[str],
    concurrency: int,
    *,
    with_scheduler: bool,
    extra_queues: list[str],
) -> list[tuple[tuple[str, ...], bool]]:
    """Build restart-stable worker specs for base and extra queues."""

    specs = [
        (tuple(queues), with_scheduler and index == 0)
        for index in range(concurrency)
    ]
    # RQ promotes delayed jobs only for queues owned by that scheduler.
    specs.extend(((name,), with_scheduler) for name in extra_queues)
    return specs


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <queue_name> [concurrency]", file=sys.stderr)
        sys.exit(1)

    queue = sys.argv[1]
    queues = queue.split(",") if "," in queue else [queue]
    extra_queues = [
        value.strip()
        for value in os.environ.get("WORKER_EXTRA_QUEUES", "").split(",")
        if value.strip() and value.strip() not in queues
    ]
    argv_concurrency = (
        sys.argv[2]
        if len(sys.argv) > 2 and not sys.argv[2].startswith("--")
        else None
    )
    with_scheduler = "--with-scheduler" in sys.argv[2:]
    _register_resource_state_bridge()

    configured_concurrency: int | None = None
    concurrency_cap: int | None = None
    if queues and queues[0].startswith("downloads"):
        configured_concurrency, concurrency_cap, concurrency = resolve_download_concurrency(
            _db_download_concurrency(), argv_concurrency
        )
        print(
            "[worker_entrypoint] downloads concurrency "
            f"configured={configured_concurrency} cap={concurrency_cap} effective={concurrency}",
            file=sys.stderr,
            flush=True,
        )
    else:
        try:
            concurrency = max(1, int(argv_concurrency)) if argv_concurrency is not None else 1
        except (TypeError, ValueError):
            concurrency = 1

    processes: list[ManagedProcess] = []
    pending_restarts: list[PendingRestart] = []
    restart_circuit = RestartCircuit()
    running = True
    last_exit: dict | None = None
    last_status_at = 0.0
    last_scheduler_watchdog_at = 0.0

    worker_specs = build_worker_specs(
        queues,
        concurrency,
        with_scheduler=with_scheduler,
        extra_queues=extra_queues,
    )
    expected_worker_count = len(worker_specs)
    supervised_queues = list(
        dict.fromkeys(name for spec, _scheduler in worker_specs for name in spec)
    )

    def spawn(worker_queues: tuple[str, ...], *, scheduler: bool) -> ManagedProcess:
        command = _worker_command(list(worker_queues), with_scheduler=scheduler)
        process = subprocess.Popen(command)
        managed = ManagedProcess(
            process=process,
            queues=worker_queues,
            with_scheduler=scheduler,
        )
        processes.append(managed)
        scheduler_note = " with scheduler" if scheduler else ""
        print(
            f"Started worker {len(processes)}/{expected_worker_count} "
            f"on queues {list(worker_queues)}"
            f"{scheduler_note} (pid={process.pid})",
            flush=True,
        )
        return managed

    def shutdown(signum, frame):
        nonlocal running
        running = False
        print(
            f"\nShutting down {len(processes)} worker(s) on queues "
            f"{supervised_queues}...",
            flush=True,
        )
        _publish_supervisor_status(
            supervised_queues,
            {
                "state": "stopping",
                "worker_count": len(processes),
                "expected_worker_count": expected_worker_count,
            },
        )
        for managed in processes:
            managed.process.terminate()
        deadline = time.monotonic() + 55
        while any(item.process.poll() is None for item in processes) and time.monotonic() < deadline:
            time.sleep(0.25)
        for managed in processes:
            if managed.process.poll() is None:
                managed.process.kill()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for worker_queues, scheduler_enabled in worker_specs:
        spawn(worker_queues, scheduler=scheduler_enabled)

    while running:
        now = time.monotonic()
        restart_circuit.refresh(now)

        if (
            "scheduled" in supervised_queues
            and now - last_scheduler_watchdog_at >= SCHEDULER_WATCHDOG_INTERVAL_SECONDS
        ):
            try:
                from app.services.scheduler_loop import scheduler_watchdog

                result = scheduler_watchdog()
                if result.get("created"):
                    print(
                        "[worker_entrypoint] scheduler watchdog restored "
                        f"{result.get('job_id')}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 - retry on next watchdog tick
                print(
                    f"[worker_entrypoint] scheduler watchdog failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            last_scheduler_watchdog_at = now
        for managed in list(processes):
            process = managed.process
            return_code = process.poll()
            if return_code is None:
                continue
            processes.remove(managed)
            last_exit = {
                "pid": process.pid,
                "return_code": return_code,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            if not running:
                continue
            delay = restart_circuit.record_exit(now)
            due_at = now + delay
            pending_restarts.append(
                PendingRestart(
                    due_at=due_at,
                    queues=managed.queues,
                    with_scheduler=managed.with_scheduler,
                )
            )
            if restart_circuit.is_open(now):
                print(
                    f"Worker pid={process.pid} exited with code {return_code}; "
                    f"restart circuit open for {int(delay)}s",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"Worker pid={process.pid} exited with code {return_code}; restarting in {int(delay)}s",
                    file=sys.stderr,
                    flush=True,
                )

        circuit_open = restart_circuit.is_open(now)
        if not circuit_open:
            for pending in list(pending_restarts):
                if pending.due_at > now:
                    continue
                # Schedulers are queue-scoped. Preserve the crashed spec even
                # when a different queue's scheduler is still alive.
                spawn(pending.queues, scheduler=pending.with_scheduler)
                pending_restarts.remove(pending)

        if len(processes) == expected_worker_count and not pending_restarts:
            restart_circuit.observe_fully_running(now)

        if now - last_status_at >= SUPERVISOR_STATUS_INTERVAL_SECONDS:
            state = "circuit_open" if circuit_open else "backoff" if pending_restarts else "running"
            circuit_open_until = None
            if restart_circuit.circuit_until is not None:
                remaining = max(0.0, restart_circuit.circuit_until - now)
                circuit_open_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=remaining)
                ).isoformat()
            _publish_supervisor_status(
                supervised_queues,
                {
                    "state": state,
                    "worker_count": len(processes),
                    "worker_pids": [item.process.pid for item in processes],
                    "configured_concurrency": configured_concurrency or concurrency,
                    "concurrency_cap": concurrency_cap,
                    "effective_concurrency": concurrency,
                    "expected_worker_count": expected_worker_count,
                    "extra_queues": extra_queues,
                    "pending_restarts": len(pending_restarts),
                    "crashes_in_window": len(restart_circuit.crashes),
                    "circuit_open_until": circuit_open_until,
                    "last_exit": last_exit,
                },
            )
            last_status_at = now
        time.sleep(1)

    for managed in processes:
        try:
            managed.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            managed.process.kill()
    print("All workers stopped.", flush=True)


if __name__ == "__main__":
    main()
