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
from typing import TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from app.services.shared_worker_process import SharedWorkerProcess
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
OUTBOX_FALLBACK_INTERVAL_SECONDS = 60
OUTBOX_HEALTH_REFRESH_INTERVAL_SECONDS = 30
IMPORT_WORKER_CONCURRENCY_ENV = "IMPORT_WORKER_CONCURRENCY"
IMPORT_WORKER_CONCURRENCY_DEFAULT = 1
IMPORT_WORKER_CONCURRENCY_MIN = 1
IMPORT_WORKER_CONCURRENCY_MAX = 4


from app.services.download_concurrency import (
    resolve_concurrency,
    resolve_concurrency_cap,
    resolve_download_concurrency as _resolve_download_concurrency,
)


def resolve_download_concurrency(db_value, argv_value) -> tuple[int, int, int]:
    """Keep the supervisor's tuple API while sharing the pure resolver."""

    from app.services.device_profile import current_device_profile

    decision = _resolve_download_concurrency(
        db_value,
        argv_value,
        cap_value=os.environ.get("DOWNLOAD_CONCURRENCY_CAP", "1"),
        automatic_device_limit=current_device_profile().download_concurrency_limit,
        explicit_device_limit=os.environ.get("DOWNLOAD_DEVICE_CONCURRENCY_LIMIT"),
    )
    return decision.configured, decision.cap, decision.effective


def _download_startup(argv_value: str | None) -> dict:
    """Run one-off imports outside the long-lived download supervisor."""

    command = [sys.executable, "-m", "app.services.download_startup"]
    if argv_value is not None:
        command.append(argv_value)
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        output, diagnostic = process.communicate(timeout=15)
        if diagnostic:
            print(diagnostic, end="", file=sys.stderr)
        if process.returncode:
            raise RuntimeError(f"download startup helper exited with code {process.returncode}")
        return json.loads(output)
    finally:
        # Also reap on timeout, malformed output, or a stop during startup.
        # No credential checker or database helper remains as a resident child.
        if process.poll() is None:
            process.kill()
            process.communicate()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


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
    process: subprocess.Popen | SharedWorkerProcess
    queues: tuple[str, ...]
    with_scheduler: bool = False


@dataclass
class PendingRestart:
    due_at: float
    queues: tuple[str, ...]
    with_scheduler: bool = False


def download_worker_environment(worker_queues, effective_concurrency: int):
    """Pass the supervisor's resolved slots to a download child only."""

    if not any(str(queue).startswith("downloads") for queue in worker_queues):
        return None
    child_env = os.environ.copy()
    child_env["DOWNLOAD_EXECUTION_CONCURRENCY"] = str(effective_concurrency)
    return child_env


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


def resolve_import_concurrency(argv_value: str | None) -> int:
    """Resolve import consumers, preferring an explicit CLI value.

    Unlike the historical generic worker fallback, malformed import values
    are configuration errors. Silently turning an invalid value into one
    consumer makes a deployment typo difficult to detect and can hide an
    unintended throughput change.
    """

    source = "CLI" if argv_value is not None else IMPORT_WORKER_CONCURRENCY_ENV
    raw_value = argv_value if argv_value is not None else os.environ.get(
        IMPORT_WORKER_CONCURRENCY_ENV, str(IMPORT_WORKER_CONCURRENCY_DEFAULT)
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{source} {IMPORT_WORKER_CONCURRENCY_ENV} must be an integer "
            f"between {IMPORT_WORKER_CONCURRENCY_MIN} and {IMPORT_WORKER_CONCURRENCY_MAX}; "
            f"got {raw_value!r}"
        ) from exc
    if not IMPORT_WORKER_CONCURRENCY_MIN <= value <= IMPORT_WORKER_CONCURRENCY_MAX:
        raise ValueError(
            f"{source} {IMPORT_WORKER_CONCURRENCY_ENV} must be between "
            f"{IMPORT_WORKER_CONCURRENCY_MIN} and {IMPORT_WORKER_CONCURRENCY_MAX}; got {value}"
        )
    return value


def _queue_worker_counts(
    processes: list[ManagedProcess],
    worker_specs: list[tuple[tuple[str, ...], bool]],
) -> dict[str, dict[str, int]]:
    """Report fixed-shape counts for the import and maintenance queues."""

    tracked_queues = ("imports", "maintenance")
    target_counts = {
        queue: sum(queue in spec_queues for spec_queues, _scheduler in worker_specs)
        for queue in tracked_queues
    }
    alive_counts = {
        queue: sum(
            queue in managed.queues and managed.process.poll() is None
            for managed in processes
        )
        for queue in tracked_queues
    }
    return {
        queue: {"alive": alive_counts[queue], "target": target_counts[queue]}
        for queue in tracked_queues
    }


def _sweep_personal_auth_startup(queues: list[str]) -> int:
    """Recover private-auth crash debris before download workers are spawned."""

    if not any(queue.startswith("downloads") for queue in queues):
        return 0
    from app.services.personal_auth_storage import (
        sweep_abandoned_personal_auth_configs,
    )

    return sweep_abandoned_personal_auth_configs()


def run_outbox_control_plane_tick(
    *,
    fallback: bool,
    refresh_health: bool,
) -> dict:
    """Run the unique scheduler-owned durable outbox maintenance tick."""

    import asyncio

    from app.database import async_session, engine
    from app.services.outbox_coordinator import (
        cleanup_completed_outboxes,
        outbox_readiness,
        publish_outbox_health,
        wake_pending_outboxes,
    )

    async def _run() -> dict:
        outcome: dict = {}
        try:
            async with async_session() as db:
                if refresh_health:
                    outcome["health"] = await publish_outbox_health(db)
                if fallback:
                    readiness = await outbox_readiness(db)
                    outcome["readiness"] = readiness
                    if any(readiness.values()):
                        outcome["wake"] = wake_pending_outboxes(readiness)
                    outcome["cleanup"] = await cleanup_completed_outboxes(db)
        finally:
            # asyncio.run creates a fresh loop for every supervisor tick.  Do
            # not retain asyncpg connections bound to the now-closed loop.
            await engine.dispose()
        return outcome

    return asyncio.run(_run())


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

    # Resolve imports before starting consumers. Each independent RQ
    # interpreter must inherit the selected CLI value, otherwise a stale
    # environment value can reject startup or select a different count.
    import_concurrency: int | None = None
    if queues and queues[0] == "imports":
        try:
            import_concurrency = resolve_import_concurrency(argv_concurrency)
        except ValueError as exc:
            print(f"[worker_entrypoint] {exc}", file=sys.stderr, flush=True)
            sys.exit(2)
        os.environ[IMPORT_WORKER_CONCURRENCY_ENV] = str(import_concurrency)

    # RQ is launched in a separate interpreter. Its ResourceAwareWorker
    # registers the TaskRun resource-state bridge before forking workhorses;
    # registering it here cannot reach those consumers and loads unused ORM
    # dependencies into an otherwise lightweight process.

    download_startup = None
    try:
        if queues and queues[0].startswith("downloads"):
            download_startup = _download_startup(argv_concurrency)
            removed_auth_configs = download_startup["removed_auth_configs"]
        else:
            removed_auth_configs = _sweep_personal_auth_startup(queues)
    except Exception as exc:
        print(
            f"[worker_entrypoint] startup validation failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    if removed_auth_configs:
        print(
            "[worker_entrypoint] removed "
            f"{removed_auth_configs} abandoned personal authentication config(s)",
            file=sys.stderr,
            flush=True,
        )

    configured_concurrency: int | None = None
    concurrency_cap: int | None = None
    if queues and queues[0].startswith("downloads"):
        configured_concurrency = download_startup["configured"]
        concurrency_cap = download_startup["cap"]
        concurrency = download_startup["effective"]
        print(
            "[worker_entrypoint] downloads concurrency "
            f"configured={configured_concurrency} cap={concurrency_cap} effective={concurrency}",
            file=sys.stderr,
            flush=True,
        )
    else:
        if queues and queues[0] == "imports":
            # Already validated before app.config was imported.
            concurrency = import_concurrency
        else:
            try:
                concurrency = max(1, int(argv_concurrency)) if argv_concurrency is not None else 1
            except (TypeError, ValueError):
                concurrency = 1

    processes: list[ManagedProcess] = []
    pending_restarts: list[PendingRestart] = []
    forced_pids: set[int] = set()
    restart_circuit = RestartCircuit()
    running = True
    last_exit: dict | None = None
    last_status_at = 0.0
    last_scheduler_watchdog_at = 0.0
    last_outbox_fallback_at = 0.0
    last_outbox_health_at = 0.0

    worker_specs = build_worker_specs(
        queues,
        concurrency,
        with_scheduler=with_scheduler,
        extra_queues=extra_queues,
    )
    shared_factory = None
    shared_mode = os.environ.get("WORKER_SHARED_INITIALIZATION", "0").strip().lower()
    if shared_mode not in {"0", "false", "1", "true"}:
        raise ValueError("WORKER_SHARED_INITIALIZATION must be 0 or 1")
    if shared_mode in {"1", "true"}:
        supported_queues = (
            queues == ["operations"] and not extra_queues
        ) or (
            queues == ["imports"] and extra_queues in ([], ["maintenance"])
        )
        if not supported_queues or concurrency < 2:
            raise ValueError(
                "Shared initialization requires multiple operations or imports consumers; "
                "only imports may include the independent maintenance queue"
            )
        from app.services.shared_worker_process import SharedWorkerFactory

        shared_factory = SharedWorkerFactory()
    fatal_shared_failure = False
    expected_worker_count = len(worker_specs)
    supervised_queues = list(
        dict.fromkeys(name for spec, _scheduler in worker_specs for name in spec)
    )

    def record_exit(managed: ManagedProcess, return_code: int) -> None:
        print(json.dumps({
            "event": "worker_stopped",
            "pid": managed.process.pid,
            "queues": list(managed.queues),
            "return_code": return_code,
            "forced": managed.process.pid in forced_pids,
        }), flush=True)

    def spawn(worker_queues: tuple[str, ...], *, scheduler: bool) -> ManagedProcess:
        command = _worker_command(list(worker_queues), with_scheduler=scheduler)
        child_env = download_worker_environment(worker_queues, concurrency)
        if child_env is None and "imports" in worker_queues:
            child_env = os.environ.copy()
            child_env[IMPORT_WORKER_CONCURRENCY_ENV] = str(concurrency)
        if shared_factory is not None:
            process = shared_factory.start(command, child_env)
        else:
            process = subprocess.Popen(command, env=child_env) if child_env is not None else subprocess.Popen(command)
        if not running:
            # A stop can arrive while a shared template is still initializing.
            # Include the just-created consumer in teardown, never leave it
            # running after the shutdown handler has finished its first pass.
            process.terminate()
        managed = ManagedProcess(
            process=process,
            queues=worker_queues,
            with_scheduler=scheduler,
        )
        processes.append(managed)
        print(json.dumps({
            "event": "worker_started", "pid": process.pid,
            "queues": list(worker_queues),
        }), flush=True)
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
                "queue_worker_counts": _queue_worker_counts(processes, worker_specs),
            },
        )
        for managed in processes:
            managed.process.terminate()
        deadline = time.monotonic() + 55
        while any(item.process.poll() is None for item in processes) and time.monotonic() < deadline:
            time.sleep(0.25)
        for managed in processes:
            if managed.process.poll() is None:
                forced_pids.add(managed.process.pid)
                managed.process.kill()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    recovery_control = None
    if "scheduled" in supervised_queues:
        from app.services.outbox_recovery_control import OutboxRecoveryControl

        recovery_control = OutboxRecoveryControl(REDIS_URL)

    try:
        for worker_queues, scheduler_enabled in worker_specs:
            if not running:
                break
            spawn(worker_queues, scheduler=scheduler_enabled)
    except BaseException:
        shutdown(signal.SIGTERM, None)
        for managed in processes:
            managed.process.wait(timeout=10)
        raise

    while running:
        if shared_factory is not None and not shared_factory.alive():
            print("[worker_entrypoint] shared initialization process exited; stopping consumers", file=sys.stderr, flush=True)
            fatal_shared_failure = True
            shutdown(signal.SIGTERM, None)
            break
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
        if "scheduled" in supervised_queues:
            recovery_control.poll()
            fallback_due = (
                now - last_outbox_fallback_at >= OUTBOX_FALLBACK_INTERVAL_SECONDS
            )
            health_due = (
                now - last_outbox_health_at
                >= OUTBOX_HEALTH_REFRESH_INTERVAL_SECONDS
            )
            if fallback_due or health_due:
                try:
                    result = run_outbox_control_plane_tick(
                        fallback=fallback_due,
                        refresh_health=health_due,
                    )
                    wake = result.get("wake") or {}
                    cleanup = result.get("cleanup") or {}
                    if wake.get("enqueued") or cleanup.get("deleted"):
                        print(
                            "[worker_entrypoint] outbox control-plane "
                            f"wake={wake} cleanup={cleanup}",
                            flush=True,
                        )
                except Exception as exc:  # noqa: BLE001 - retry next tick
                    print(
                        f"[worker_entrypoint] outbox control-plane failed: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                if fallback_due:
                    last_outbox_fallback_at = now
                if health_due:
                    last_outbox_health_at = now
        for managed in list(processes):
            process = managed.process
            return_code = process.poll()
            if return_code is None:
                continue
            processes.remove(managed)
            record_exit(managed, return_code)
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
                try:
                    spawn(pending.queues, scheduler=pending.with_scheduler)
                except Exception as exc:
                    if shared_factory is None:
                        raise
                    print(f"[worker_entrypoint] shared consumer restart failed: {type(exc).__name__}", file=sys.stderr, flush=True)
                    fatal_shared_failure = True
                    shutdown(signal.SIGTERM, None)
                    break
                pending_restarts.remove(pending)

        if not running:
            break
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
                    "queue_worker_counts": _queue_worker_counts(processes, worker_specs),
                    "extra_queues": extra_queues,
                    "pending_restarts": len(pending_restarts),
                    "crashes_in_window": len(restart_circuit.crashes),
                    "circuit_open_until": circuit_open_until,
                    "last_exit": last_exit,
                },
            )
            last_status_at = now
        time.sleep(1)

    if recovery_control is not None:
        recovery_control.close()
    for managed in processes:
        try:
            return_code = managed.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forced_pids.add(managed.process.pid)
            managed.process.kill()
            # Do not claim success before the killed child has been reaped.
            return_code = managed.process.wait(timeout=10)
        record_exit(managed, return_code)
    if shared_factory is not None:
        shared_factory.close()
    print("All workers stopped.", flush=True)
    if fatal_shared_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
