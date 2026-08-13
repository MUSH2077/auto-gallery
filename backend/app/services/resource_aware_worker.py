"""RQ worker that leaves queued jobs untouched while the NAS is pressured."""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone

import redis
from rq import Worker
from rq.worker import WorkerStatus

from app.config import settings
from app.services.heavy_io import (
    HEAVY_IO_LOCK_KEY,
    RESOURCE_DISK_TOKEN_KEY,
    RESOURCE_NETWORK_TOKEN_KEY,
    RenewableRedisLease,
    local_lock_for_workload,
    mark_worker_flock_inherited,
    register_resource_state_callback,
    resource_budget_token_key,
    resource_lease_keys,
    resource_reservation_details,
)
from app.services.resource_pressure import get_resource_pressure_snapshot_sync
from app.services.resource_pressure import (
    RESOURCE_CONTROL_CHANNEL,
    profile_slice_cooldown_seconds,
    publish_external_resource_critical,
    resource_profile_permit,
    sample_cgroup_contribution,
    workload_profile_name,
)

REDIS_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
REDIS_CONNECTION_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
HEAVY_QUEUE_NAMES = frozenset({"imports", "operations", "maintenance"})
RESOURCE_WORK_CHANNEL_PREFIX = "resource:work:"
IDLE_WAIT_MIN_SECONDS = 2.0
IDLE_WAIT_MAX_SECONDS = 30.0
WORKER_STATE_HEARTBEAT_SECONDS = 30.0


def _set_resource_state_sync(
    owner: str,
    state: str,
    reason: str | None,
    *,
    workload: str,
) -> None:
    """Persist one parent-worker transition without crossing event loops.

    RQ's parent process performs admission before forking each workhorse. These
    callbacks are synchronous and therefore use a fresh ``asyncio.run`` loop.
    Dispose SQLAlchemy's async pool on that same loop so the next job cannot
    receive an asyncpg connection created by the previous, already-closed loop.
    """

    from app.database import engine
    from app.services.heavy_io import set_resource_state

    async def _persist() -> None:
        try:
            await set_resource_state(
                owner,
                state,
                reason,
                workload=workload,
            )
        finally:
            await engine.dispose()

    asyncio.run(_persist())


def redis_retry_delay(failure_count: int) -> float:
    """Capped exponential delay; retries continue without exiting the worker."""

    index = max(0, min(failure_count - 1, len(REDIS_RETRY_DELAYS) - 1))
    return REDIS_RETRY_DELAYS[index]


def adaptive_wait_delay(attempt: int, *, jitter: bool = True) -> float:
    # Short-circuit before exponentiation.  RQ workers can remain constrained
    # for days, making ``attempt`` arbitrarily large; computing 2 ** attempt
    # first used to overflow while the intended result was already the cap.
    safe_attempt = max(0, int(attempt))
    if safe_attempt >= 4:
        base = IDLE_WAIT_MAX_SECONDS
    else:
        base = min(
            IDLE_WAIT_MAX_SECONDS,
            IDLE_WAIT_MIN_SECONDS * (2 ** safe_attempt),
        )
    if not jitter:
        return base
    return max(IDLE_WAIT_MIN_SECONDS, min(IDLE_WAIT_MAX_SECONDS, base * random.uniform(0.85, 1.15)))


class ResourceAwareWorker(Worker):
    """Pause before dequeueing; an already-running job is never interrupted."""

    def __init__(self, *args, **kwargs):
        # This runs in the RQ worker interpreter (unlike worker_entrypoint,
        # which execs the rq CLI).  The subsequent workhorse fork therefore
        # inherits exactly one registered TaskRun bridge.
        try:
            from app.services.tasks import update_task_resource_state

            register_resource_state_callback(update_task_resource_state)
        except Exception:
            # Rolling upgrades can temporarily load a worker before the TaskRun
            # migration/code is present; RQ metadata remains the fallback.
            register_resource_state_callback(None)
        super().__init__(*args, **kwargs)

    def _publish_pressure_state(self, snapshot: dict) -> dict[str, object]:
        """Publish a low-write worker view and close worker-local cgroup gaps.

        The backend monitor can only see its own cgroup.  Every worker therefore
        treats a newly observed ``oom_kill`` or ``max`` event in its cgroup as
        an immediate hard gate and promotes it into the shared latch.  A plain
        ``oom`` allocation failure without either boundary event remains soft
        evidence and temporarily halves bounded work slices.
        """

        now = time.monotonic()
        cgroup_contribution = sample_cgroup_contribution()
        cgroup_events = cgroup_contribution.get("memory_events") or {}
        previous_events = getattr(self, "_last_cgroup_events", None)
        cgroup_deltas = {
            name: (
                # A replacement RQ child is commonly spawned inside the same
                # container cgroup after its predecessor was OOM-killed.  The
                # first sample must therefore treat existing counters as new
                # evidence instead of resetting the baseline to zero.
                max(0, int(cgroup_events.get(name) or 0))
                if previous_events is None
                else max(
                    0,
                    int(cgroup_events.get(name) or 0)
                    - int(previous_events.get(name) or 0),
                )
            )
            for name in ("max", "oom", "oom_kill")
        }
        # Advance the local baseline even when Redis is unavailable.  Repeating
        # the same cumulative event would otherwise cause a publish storm.
        self._last_cgroup_events = cgroup_events

        if cgroup_deltas["oom"] > 0:
            self._local_cgroup_soft_until = max(
                getattr(self, "_local_cgroup_soft_until", 0.0),
                now + 60.0,
            )

        hard_event_reason = (
            "worker_cgroup_oom_kill"
            if cgroup_deltas["oom_kill"] > 0
            else "worker_cgroup_memory_max"
            if cgroup_deltas["max"] > 0
            else None
        )
        if hard_event_reason is not None:
            self._local_cgroup_oom_kill_latched = True
            self._local_cgroup_oom_kill_at = now
            self._local_cgroup_hard_reason = hard_event_reason
            try:
                external = publish_external_resource_critical(
                    hard_event_reason,
                    redis_client=self.connection,
                    source=str(self.name),
                )
                snapshot.clear()
                snapshot.update(external)
            except Exception:
                self.log.exception(
                    "Unable to promote worker cgroup memory event; local gate remains closed"
                )

        local_latched = bool(
            getattr(self, "_local_cgroup_oom_kill_latched", False)
        )
        if local_latched:
            event_at = float(getattr(self, "_local_cgroup_oom_kill_at", now))
            shared_recovered = (
                snapshot.get("status") != "paused"
                and snapshot.get("controller_mode") != "critical"
            )
            if (
                shared_recovered
                and now - event_at >= max(0.0, settings.resource_pressure_resume_seconds)
            ):
                self._local_cgroup_oom_kill_latched = False
                self._local_cgroup_hard_reason = None
                local_latched = False
            else:
                snapshot["status"] = "paused"
                snapshot["controller_mode"] = "critical"
                snapshot["reasons"] = list(
                    dict.fromkeys(
                        [
                            *(snapshot.get("reasons") or []),
                            str(
                                getattr(
                                    self,
                                    "_local_cgroup_hard_reason",
                                    "worker_cgroup_oom_kill",
                                )
                                or "worker_cgroup_oom_kill"
                            ),
                        ]
                    )
                )

        local_soft_scale = (
            0.5
            if now < float(getattr(self, "_local_cgroup_soft_until", 0.0))
            else 1.0
        )
        if local_soft_scale < 1.0:
            snapshot["reasons"] = list(
                dict.fromkeys(
                    [
                        *(snapshot.get("reasons") or []),
                        "worker_cgroup_memory_pressure",
                    ]
                )
            )

        budget = snapshot.get("budget") or {}
        cgroup_signature = tuple(cgroup_events.get(name) for name in ("max", "oom", "oom_kill"))
        signature = (
            str(snapshot.get("status") or "unknown"),
            str(snapshot.get("controller_mode") or "unknown"),
            tuple(snapshot.get("reasons") or []),
            budget.get("generation"),
            budget.get("throughput_scale"),
            cgroup_signature,
            local_latched,
            local_soft_scale,
        )
        if (
            signature == getattr(self, "_last_pressure_signature", None)
            and now - getattr(self, "_last_pressure_publish_at", 0.0)
            < WORKER_STATE_HEARTBEAT_SECONDS
        ):
            return {
                "hard_gate_active": local_latched,
                "soft_scale": local_soft_scale,
                "cgroup_deltas": cgroup_deltas,
            }
        try:
            previous_contribution = getattr(self, "_last_cgroup_contribution", None)
            previous_at = float(
                getattr(self, "_last_cgroup_contribution_at", now)
            )
            elapsed = max(0.001, now - previous_at)

            def metric(section: str, name: str) -> int | float | None:
                value = (cgroup_contribution.get(section) or {}).get(name)
                return value if isinstance(value, (int, float)) else None

            def previous_metric(section: str, name: str) -> int | float | None:
                if not isinstance(previous_contribution, dict):
                    return None
                value = (previous_contribution.get(section) or {}).get(name)
                return value if isinstance(value, (int, float)) else None

            def delta(section: str, name: str) -> int | None:
                current = metric(section, name)
                previous = previous_metric(section, name)
                if current is None or previous is None:
                    return None
                return max(0, int(current) - int(previous))

            def rate(section: str, name: str) -> float | None:
                value = delta(section, name)
                return value / elapsed if value is not None else None

            cpu_usage_delta = delta("cpu", "usage_usec")
            cpu_usage_cores = (
                cpu_usage_delta / (elapsed * 1_000_000)
                if cpu_usage_delta is not None
                else None
            )

            def psi_value(resource: str, kind: str, window: str) -> float | None:
                value = (
                    ((cgroup_contribution.get("psi") or {}).get(resource) or {})
                    .get(kind, {})
                    .get(window)
                )
                return value if isinstance(value, (int, float)) else None

            contribution_fields = {
                "cgroup_sampled_at": datetime.now(timezone.utc).isoformat(),
                "cgroup_id": str(cgroup_contribution.get("cgroup_id") or "unknown"),
                "cgroup_metrics_available": (
                    "1" if metric("memory", "current_bytes") is not None else "0"
                ),
                "cgroup_rate_interval_seconds": str(round(elapsed, 3)),
                "cgroup_memory_current_bytes": str(metric("memory", "current_bytes") or 0),
                "cgroup_memory_peak_bytes": str(metric("memory", "peak_bytes") or 0),
                "cgroup_memory_limit_bytes": str(metric("memory", "limit_bytes") or 0),
                "cgroup_cpu_usage_usec": str(metric("cpu", "usage_usec") or 0),
                "cgroup_cpu_user_usec": str(metric("cpu", "user_usec") or 0),
                "cgroup_cpu_system_usec": str(metric("cpu", "system_usec") or 0),
                "cgroup_cpu_usage_usec_delta": str(cpu_usage_delta or 0),
                "cgroup_cpu_usage_cores": str(cpu_usage_cores or 0.0),
                "cgroup_cpu_nr_throttled": str(metric("cpu", "nr_throttled") or 0),
                "cgroup_cpu_nr_throttled_delta": str(delta("cpu", "nr_throttled") or 0),
                "cgroup_cpu_throttled_usec": str(metric("cpu", "throttled_usec") or 0),
                "cgroup_cpu_throttled_usec_delta": str(delta("cpu", "throttled_usec") or 0),
                "cgroup_io_read_bytes": str(metric("io", "read_bytes") or 0),
                "cgroup_io_read_bytes_delta": str(delta("io", "read_bytes") or 0),
                "cgroup_io_read_bytes_per_second": str(rate("io", "read_bytes") or 0.0),
                "cgroup_io_write_bytes": str(metric("io", "write_bytes") or 0),
                "cgroup_io_write_bytes_delta": str(delta("io", "write_bytes") or 0),
                "cgroup_io_write_bytes_per_second": str(rate("io", "write_bytes") or 0.0),
                "cgroup_io_read_ios": str(metric("io", "read_ios") or 0),
                "cgroup_io_write_ios": str(metric("io", "write_ios") or 0),
            }
            for resource in ("memory", "io", "cpu"):
                for kind in ("some", "full"):
                    for window in ("avg10", "avg60", "avg300"):
                        contribution_fields[
                            f"cgroup_psi_{resource}_{kind}_{window}"
                        ] = str(psi_value(resource, kind, window) or 0.0)

            self.connection.hset(
                self.key,
                mapping={
                    "resource_pressure_status": str(snapshot.get("status") or "unknown"),
                    "resource_controller_mode": str(
                        snapshot.get("controller_mode") or "unknown"
                    ),
                    "resource_pressure_reasons": ",".join(snapshot.get("reasons") or []),
                    "resource_throughput_scale": str(
                        budget.get("throughput_scale")
                        if budget.get("throughput_scale") is not None
                        else "unknown"
                    ),
                    "resource_local_throughput_scale": str(local_soft_scale),
                    "resource_local_hard_gate": "1" if local_latched else "0",
                    "cgroup_memory_max_events": str(cgroup_events.get("max") or 0),
                    "cgroup_memory_oom_events": str(cgroup_events.get("oom") or 0),
                    "cgroup_memory_oom_kill_events": str(
                        cgroup_events.get("oom_kill") or 0
                    ),
                    "cgroup_memory_max_delta": str(cgroup_deltas["max"]),
                    "cgroup_memory_oom_delta": str(cgroup_deltas["oom"]),
                    "cgroup_memory_oom_kill_delta": str(cgroup_deltas["oom_kill"]),
                    **contribution_fields,
                },
            )
            self._last_pressure_signature = signature
            self._last_pressure_publish_at = now
            self._last_cgroup_contribution = cgroup_contribution
            self._last_cgroup_contribution_at = now
        except Exception:
            self.log.debug("Unable to publish worker pressure state", exc_info=True)
        return {
            "hard_gate_active": local_latched,
            "soft_scale": local_soft_scale,
            "cgroup_deltas": cgroup_deltas,
        }

    @staticmethod
    def _is_heavy_queue(queue) -> bool:
        name = str(getattr(queue, "name", ""))
        return name in HEAVY_QUEUE_NAMES or name == "downloads" or name.startswith("downloads:")

    def _uses_heavy_gate(self) -> bool:
        queues = tuple(getattr(self, "queues", ()))
        heavy_flags = tuple(self._is_heavy_queue(queue) for queue in queues)
        if any(heavy_flags) and not all(heavy_flags):
            names = ",".join(str(getattr(queue, "name", "")) for queue in queues)
            raise RuntimeError(f"ResourceAwareWorker cannot mix heavy and non-heavy queues: {names}")
        return bool(heavy_flags and all(heavy_flags))

    def _workload_profile(self) -> str:
        names = {str(getattr(queue, "name", "")) for queue in self.queues}
        if names and all(name == "downloads" or name.startswith("downloads:") for name in names):
            return "download_network"
        if names == {"imports"}:
            return "import_db"
        # Operations is heterogeneous.  It is admitted after dequeue once the
        # concrete func/meta profile is known.
        return "light"

    @staticmethod
    def _internal_slice_workload(job) -> str | None:
        """Return the child-owned profile for cooperative coordinator jobs.

        These jobs must be admitted against their real resource profile before
        dequeue, but the parent must not hold that profile across the whole RQ
        workhorse.  The child acquires and renews one bounded slice at a time.
        """

        func_name = str(getattr(job, "func_name", "") or "").lower()
        profiles = (
            ("run_clear_operation", "maintenance"),
            ("run_library_rebuild_operation", "maintenance"),
            ("run_disk_import_operation", "maintenance"),
            ("run_gitllery_rebuild_operation", "maintenance"),
            ("run_creator_reenrich_operation", "maintenance"),
            ("run_gitllery_sync_operation", "git_projection"),
            ("run_curation_backfill_operation", "import_db"),
            ("run_search_reindex_operation", "search_index"),
            ("import_runner.run_import_job", "import_db"),
            ("run_asset_dedup_scan", "image_derive"),
            ("run_asset_dedup_outbox", "image_derive"),
            ("run_media_derivative_outbox", "image_derive"),
            ("run_gitllery_projection_outbox", "git_projection"),
            ("run_import_projection_outbox", "import_db"),
            ("run_search_projection_outbox", "search_index"),
        )
        for name, profile in profiles:
            if name in func_name:
                return profile
        return None

    @classmethod
    def _job_workload(cls, job, queue) -> str:
        metadata = getattr(job, "meta", None) or {}
        internal_profile = cls._internal_slice_workload(job)
        if internal_profile is not None:
            # Override persisted whole-job metadata during rolling upgrades.
            # Admission still uses the real profile, while execute_job avoids
            # taking a parent lease for internally sliced coordinators.
            return internal_profile
        explicit = metadata.get("resource_profile") or metadata.get("workload_profile")
        if explicit:
            return str(explicit)
        queue_name = str(getattr(queue, "name", ""))
        if queue_name == "imports":
            return "import_db"
        if queue_name == "downloads" or queue_name.startswith("downloads:"):
            return "download_network"
        return "maintenance"

    @staticmethod
    def _job_uses_nonblocking_child_admission(job) -> bool:
        """Whether a coordinator yields when its child permit is unavailable."""

        func_name = str(getattr(job, "func_name", "") or "").lower()
        return any(
            name in func_name
            for name in (
                "run_asset_dedup_scan",
                "run_asset_dedup_outbox",
                "run_media_derivative_outbox",
                "run_gitllery_projection_outbox",
                "run_import_projection_outbox",
                "run_search_projection_outbox",
            )
        )

    @staticmethod
    def _bound_job_slice(job, workload: str) -> None:
        """Clamp outbox work in-memory before the workhorse fork."""

        del workload
        func_name = str(getattr(job, "func_name", "") or "").lower()
        args = list(getattr(job, "args", ()) or ())
        if "media_derivatives" in func_name:
            args = [
                min(int(args[0]), 1) if args else 1,
                min(float(args[1]), 20.0) if len(args) > 1 else 20.0,
            ]
        elif "run_asset_dedup_outbox" in func_name:
            args = [1]
        elif "import_projection" in func_name:
            args = [
                min(int(args[0]), 25) if args else 25,
                min(float(args[1]), 20.0) if len(args) > 1 else 20.0,
            ]
        elif "gitllery_projection" in func_name:
            args = [
                min(int(args[0]), 1) if args else 1,
                min(float(args[1]), 20.0) if len(args) > 1 else 20.0,
            ]
        elif "search_projection" in func_name:
            args = [min(int(args[0]), 500) if args else 500]
        else:
            return
        job.args = tuple(args)

    def _apply_profile_slice(self, job, workload: str, snapshot: dict) -> None:
        """Apply enforce-mode AIMD and worker-local soft feedback to one slice."""

        budget = snapshot.get("budget") or {}
        profiles = budget.get("profiles") or {}
        profile = profiles.get(workload_profile_name(workload)) or {}
        if not isinstance(profile, dict) or "work_units" not in profile:
            return

        func_name = str(getattr(job, "func_name", "") or "").lower()
        args = list(getattr(job, "args", ()) or ())
        try:
            units = max(1, int(profile.get("work_units") or 1))
        except (TypeError, ValueError):
            units = 1
        slice_seconds = profile.get("slice_seconds")
        try:
            slice_seconds = max(1.0, float(slice_seconds))
        except (TypeError, ValueError):
            slice_seconds = None

        governance_mode = str(budget.get("governance_mode") or "enforce").lower()
        if (
            governance_mode == "enforce"
            and time.monotonic() < float(getattr(self, "_local_cgroup_soft_until", 0.0))
        ):
            units = max(1, int(units * 0.5))
            if slice_seconds is not None:
                slice_seconds = max(1.0, slice_seconds * 0.5)

        if "media_derivatives" in func_name:
            current_seconds = float(args[1]) if len(args) > 1 else 20.0
            args = [
                1,
                min(current_seconds, slice_seconds) if slice_seconds else current_seconds,
            ]
        elif "import_projection" in func_name or "gitllery_projection" in func_name:
            default_units = 1 if "gitllery_projection" in func_name else 25
            current_units = int(args[0]) if args else default_units
            current_seconds = float(args[1]) if len(args) > 1 else 20.0
            args = [
                min(current_units, units),
                min(current_seconds, slice_seconds) if slice_seconds else current_seconds,
            ]
        elif "search_projection" in func_name:
            current_units = int(args[0]) if args else 500
            args = [min(current_units, units)]
        else:
            return
        job.args = tuple(args)

    def _close_control_pubsub(self) -> None:
        pubsub = getattr(self, "_resource_control_pubsub", None)
        self._resource_control_pubsub = None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                self.log.debug("Unable to close resource control subscriber", exc_info=True)

    def _wait_for_control_event(self, seconds: float, workload: str) -> None:
        """Block on a control/work event, with timeout as the recovery fallback."""

        try:
            pubsub = getattr(self, "_resource_control_pubsub", None)
            if pubsub is None:
                pubsub = self.connection.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(
                    RESOURCE_CONTROL_CHANNEL,
                    f"{RESOURCE_WORK_CHANNEL_PREFIX}{workload}",
                )
                self._resource_control_pubsub = pubsub
            pubsub.get_message(timeout=max(0.05, seconds))
            return
        except (AttributeError, *REDIS_CONNECTION_ERRORS):
            self._close_control_pubsub()
        except Exception:
            self._close_control_pubsub()
            self.log.debug("Resource control event wait failed", exc_info=True)
        time.sleep(max(0.05, seconds))

    def _wait_until_pressure_allows_dequeue(
        self,
        interval: float,
        *,
        workload: str,
        job=None,
        owner: str | None = None,
        hard_only: bool = False,
    ) -> dict:
        """Remain alive but idle until this profile receives a permit."""

        logged_pause = False
        redis_failures = 0
        wait_attempt = 0
        last_bookkeeping = 0.0
        last_job_reason: str | None = None
        while True:
            snapshot_readable = True
            try:
                snapshot = get_resource_pressure_snapshot_sync(redis_client=self.connection)
            except REDIS_CONNECTION_ERRORS as exc:
                redis_failures += 1
                snapshot_readable = False
                snapshot = {
                    "status": "paused",
                    "controller_mode": "critical",
                    "reasons": ["redis_unavailable"],
                }
                self.log.warning(
                    "Worker %s: Redis unavailable while reading host pressure; "
                    "remaining paused (attempt=%d, error=%s)",
                    self.name,
                    redis_failures,
                    type(exc).__name__,
                )
            permitted, permit = resource_profile_permit(snapshot, workload)
            if hard_only:
                controller_mode = str(
                    snapshot.get("controller_mode")
                    or (
                        "critical"
                        if snapshot.get("status") == "paused"
                        else "normal"
                    )
                ).lower()
                if (
                    controller_mode != "critical"
                    and snapshot.get("status") != "paused"
                ):
                    permitted = True
                    permit = {
                        **permit,
                        "allowed": True,
                        "reason": None,
                        "hard_only": True,
                    }
            snapshot["active_profile"] = permit
            local_feedback = self._publish_pressure_state(snapshot)
            if local_feedback["hard_gate_active"]:
                permitted = False
                permit = {
                    **permit,
                    "allowed": False,
                    "reason": "worker_cgroup_oom_kill",
                }
                snapshot["active_profile"] = permit
            if permitted:
                if logged_pause:
                    self.log.info(
                        "Worker %s: resource permit restored profile=%s scale=%s",
                        self.name,
                        workload,
                        permit.get("throughput_scale"),
                    )
                if redis_failures:
                    self.log.info("Worker %s: Redis connection recovered", self.name)
                return snapshot

            wait_reason = str(permit.get("reason") or "resource_pressure")
            if job is not None and owner is not None and wait_reason != last_job_reason:
                self._set_job_resource_meta(job, workload, "waiting", wait_reason)
                _set_resource_state_sync(
                    owner,
                    "waiting",
                    wait_reason,
                    workload=workload,
                )
                last_job_reason = wait_reason

            if not logged_pause:
                self.log.warning(
                    "Worker %s: dequeue waiting for profile=%s reason=%s pressure=%s",
                    self.name,
                    workload,
                    wait_reason,
                    ", ".join(snapshot.get("reasons") or ["unspecified"]),
                )
                logged_pause = True
            pause_bookkeeping_ok = True
            now = time.monotonic()
            if now - last_bookkeeping >= WORKER_STATE_HEARTBEAT_SECONDS or last_bookkeeping == 0:
                try:
                    self.set_state(WorkerStatus.IDLE)
                    self.procline(f"Waiting: {workload} resource budget")
                    self.heartbeat()
                    if self.should_run_maintenance_tasks:
                        self.run_maintenance_tasks()
                    last_bookkeeping = now
                except REDIS_CONNECTION_ERRORS as exc:
                    redis_failures += 1
                    pause_bookkeeping_ok = False
                    self.log.warning(
                        "Worker %s: Redis unavailable during resource wait heartbeat; "
                        "retrying in-process (attempt=%d, error=%s)",
                        self.name,
                        redis_failures,
                        type(exc).__name__,
                    )
            if snapshot_readable and pause_bookkeeping_ok:
                if redis_failures:
                    self.log.info("Worker %s: Redis connection recovered while paused", self.name)
                redis_failures = 0
            delay = (
                redis_retry_delay(redis_failures)
                if redis_failures
                else adaptive_wait_delay(wait_attempt)
            )
            wait_attempt += 1
            self._wait_for_control_event(delay, workload)

    def _heavy_queues_have_jobs(self) -> bool:
        queue_keys = [queue.key for queue in self.queues if self._is_heavy_queue(queue)]
        return bool(queue_keys and self.connection.exists(*queue_keys))

    def _peek_heterogeneous_head(self):
        """Classify a heterogeneous FIFO head without removing it from Redis.

        Operations is intentionally heterogeneous.  Waiting after dequeue
        creates a crash window where a queued job is no longer present in the
        list, so admission first peeks one job.  Exact admission is repeated
        after dequeue to close the harmless multi-worker race.
        """

        for queue in self.queues:
            if str(getattr(queue, "name", "")) not in {
                "operations",
                "maintenance",
            }:
                continue
            try:
                jobs = queue.get_jobs(offset=0, length=1)
            except Exception:
                self.log.debug("Unable to peek operations queue head", exc_info=True)
                return None, queue
            return (jobs[0] if jobs else None), queue
        return None, None

    def _dequeue_heavy_job(self, timeout, max_idle_time, interval: float):
        """Dequeue without a global lock; exact profile admission follows."""

        idle_started = time.monotonic()
        last_bookkeeping = 0.0
        redis_failures = 0
        wait_attempt = 0
        workload = self._workload_profile()
        while True:
            if workload != "light":
                self._wait_until_pressure_allows_dequeue(interval, workload=workload)

            if max_idle_time is not None and time.monotonic() - idle_started >= max_idle_time:
                return None
            now = time.monotonic()
            if now - last_bookkeeping >= WORKER_STATE_HEARTBEAT_SECONDS or last_bookkeeping == 0:
                try:
                    self.set_state(WorkerStatus.IDLE)
                    self.heartbeat()
                    if self.should_run_maintenance_tasks:
                        self.run_maintenance_tasks()
                    last_bookkeeping = now
                except REDIS_CONNECTION_ERRORS as exc:
                    redis_failures += 1
                    delay = redis_retry_delay(redis_failures)
                    self.log.warning(
                        "Worker %s: Redis unavailable during heavy idle heartbeat; "
                        "retrying in %.1fs (%s)",
                        self.name,
                        delay,
                        type(exc).__name__,
                    )
                    self._wait_for_control_event(delay, workload)
                    continue

            try:
                has_jobs = self._heavy_queues_have_jobs()
            except REDIS_CONNECTION_ERRORS as exc:
                redis_failures += 1
                delay = redis_retry_delay(redis_failures)
                self.log.warning(
                    "Worker %s: Redis unavailable before heavy dequeue; retrying in %.1fs (%s)",
                    self.name,
                    delay,
                    type(exc).__name__,
                )
                self._wait_for_control_event(delay, workload)
                continue
            redis_failures = 0

            if not has_jobs:
                if timeout is None:
                    return None
                self._wait_for_control_event(adaptive_wait_delay(wait_attempt), workload)
                wait_attempt += 1
                continue

            if workload == "light":
                peeked_job, peeked_queue = self._peek_heterogeneous_head()
                if peeked_job is not None and peeked_queue is not None:
                    peeked_workload = self._job_workload(peeked_job, peeked_queue)
                    self._wait_until_pressure_allows_dequeue(
                        interval,
                        workload=peeked_workload,
                        job=peeked_job,
                        owner=self._job_owner(peeked_job),
                        hard_only=self._job_uses_nonblocking_child_admission(
                            peeked_job
                        ),
                    )
                else:
                    # A transient fetch failure must not bypass the hard gate.
                    self._wait_until_pressure_allows_dequeue(
                        interval,
                        workload="maintenance",
                    )

            try:
                result = super().dequeue_job_and_maintain_ttl(
                    1 if timeout is not None else None,
                    max_idle_time=1,
                )
                if result is None:
                    if timeout is None:
                        return None
                    continue
                return result
            except REDIS_CONNECTION_ERRORS as exc:
                redis_failures += 1
                delay = redis_retry_delay(redis_failures)
                self.log.warning(
                    "Worker %s: Redis unavailable during heavy dequeue; retrying in %.1fs (%s)",
                    self.name,
                    delay,
                    type(exc).__name__,
                )
                self._wait_for_control_event(delay, workload)

    @staticmethod
    def _job_owner(job) -> str:
        from uuid import UUID

        metadata = getattr(job, "meta", None) or {}
        for value in (
            metadata.get("owner"),
            metadata.get("task_id"),
            *(getattr(job, "args", ()) or ()),
        ):
            try:
                return str(UUID(str(value)))
            except (TypeError, ValueError):
                continue
        return str(job.id)

    @staticmethod
    def _set_job_resource_meta(job, workload: str, state: str, reason: str | None) -> None:
        metadata = getattr(job, "meta", None)
        if not isinstance(metadata, dict):
            metadata = {}
            job.meta = metadata
        current = metadata.get("resource_governance") or {}
        if (
            current.get("workload") == workload
            and current.get("state") == state
            and current.get("reason") == reason
        ):
            return
        metadata["resource_profile"] = workload
        metadata["resource_state"] = state
        metadata["resource_governance"] = {
            "workload": workload,
            "state": state,
            "reason": reason,
        }
        try:
            job.save_meta()
        except Exception:
            pass

    def _profile_admission(self, job, workload: str):
        """Wait without a lock, then hold one profile lease for this slice."""

        from app.services.heavy_io import set_resource_state

        owner = self._job_owner(job)
        attempt = 0
        while True:
            snapshot = self._wait_until_pressure_allows_dequeue(
                settings.resource_pressure_sample_interval_seconds,
                workload=workload,
                job=job,
                owner=owner,
                hard_only=self._job_uses_nonblocking_child_admission(job),
            )
            nonblocking_child = self._job_uses_nonblocking_child_admission(job)
            if (
                workload_profile_name(workload) != "maintenance"
                and not nonblocking_child
            ):
                try:
                    maintenance_pending = bool(self.connection.exists(HEAVY_IO_LOCK_KEY))
                except REDIS_CONNECTION_ERRORS:
                    maintenance_pending = True
                if maintenance_pending:
                    self._set_job_resource_meta(
                        job,
                        workload,
                        "waiting",
                        "maintenance_pending",
                    )
                    self._wait_for_control_event(adaptive_wait_delay(attempt), workload)
                    attempt += 1
                    continue

            internally_sliced = self._internal_slice_workload(job) is not None
            if internally_sliced:
                # Child-side slices own and renew the real profile lease.  The
                # parent only performs the pre-dequeue hard admission above;
                # it must not retain a second Redis/POSIX lease across the
                # coordinator or apply AIMD twice.
                self._active_profile_snapshot = {}
                self._set_job_resource_meta(job, workload, "running", None)
                _set_resource_state_sync(
                    owner, "running", None, workload=workload
                )
                return None, None, owner

            lease = None
            # Download work is decorated and acquires/renews its network plus
            # source token inside the forked workhorse.  Starting a renewal
            # thread in the parent before fork would be unsafe.
            lease_keys = (
                []
                if workload_profile_name(workload) == "download_network"
                else resource_lease_keys(workload)
            )
            if lease_keys:
                reservation_bytes, reservation_capacity = resource_reservation_details(
                    snapshot,
                    workload,
                )
                if reservation_bytes > reservation_capacity:
                    lease_reason = "profile_memory_reserve_live"
                    self._set_job_resource_meta(job, workload, "waiting", lease_reason)
                    _set_resource_state_sync(
                        owner,
                        "waiting",
                        lease_reason,
                        workload=workload,
                    )
                    self._wait_for_control_event(adaptive_wait_delay(attempt), workload)
                    attempt += 1
                    continue
                budget_token = resource_budget_token_key(workload)
                lease = RenewableRedisLease(
                    lease_keys,
                    workload=workload,
                    owner=owner,
                    reservation_key=(
                        budget_token
                        if budget_token in {
                            RESOURCE_NETWORK_TOKEN_KEY,
                            RESOURCE_DISK_TOKEN_KEY,
                        }
                        else None
                    ),
                    reservation_bytes=reservation_bytes,
                    reservation_capacity_bytes=reservation_capacity,
                )
                if not asyncio.run(lease.try_acquire()):
                    lease_reason = lease.denial_reason or "profile_lease_busy"
                    self._set_job_resource_meta(job, workload, "waiting", lease_reason)
                    _set_resource_state_sync(
                        owner,
                        "waiting",
                        lease_reason,
                        workload=workload,
                    )
                    self._wait_for_control_event(adaptive_wait_delay(attempt), workload)
                    attempt += 1
                    continue

            local_lock = local_lock_for_workload(workload)
            try:
                local_acquired = local_lock is None or local_lock.try_acquire()
            except OSError:
                local_acquired = False
            if not local_acquired:
                if lease is not None:
                    asyncio.run(lease.release())
                self._set_job_resource_meta(job, workload, "waiting", "profile_lock_busy")
                self._wait_for_control_event(adaptive_wait_delay(attempt), workload)
                attempt += 1
                continue

            # Do not start a Python thread before RQ forks its workhorse.  The
            # profile POSIX lock is authoritative on this single-host NAS; the
            # short Redis key is only admission visibility/rolling compatibility.
            self._apply_profile_slice(job, workload, snapshot)
            self._active_profile_snapshot = snapshot
            self._set_job_resource_meta(job, workload, "running", None)
            _set_resource_state_sync(owner, "running", None, workload=workload)
            return local_lock, lease, owner

    def execute_job(self, job, queue):
        """Select and hold only the concrete job's profile for one slice."""

        if not self._is_heavy_queue(queue):
            return super().execute_job(job, queue)

        workload = self._job_workload(job, queue)
        internally_sliced = self._internal_slice_workload(job) is not None
        self._bound_job_slice(job, workload)
        local_lock, lease, owner = self._profile_admission(job, workload)
        profile_snapshot = getattr(self, "_active_profile_snapshot", {})
        slice_started = time.monotonic()
        inherited_profile = workload_profile_name(workload)
        try:
            self._close_control_pubsub()
            mark_worker_flock_inherited(
                inherited_profile if local_lock is not None else False
            )
            try:
                return super().execute_job(job, queue)
            finally:
                mark_worker_flock_inherited(False)
        finally:
            if lease is not None:
                asyncio.run(lease.release())
            if local_lock is not None:
                local_lock.release()
            try:
                job.refresh()
            except Exception:
                pass
            self._set_job_resource_meta(job, workload, "yielded", "slice_complete")
            _set_resource_state_sync(
                owner,
                "yielded",
                "slice_complete",
                workload=workload,
            )
            # Batch shrinking limits peak cost but cannot lower the sustained
            # rate of a permanently backlogged queue.  Sleep only after the
            # workhorse exits and both Redis/POSIX leases are released.
            delay = (
                0.0
                if internally_sliced
                else profile_slice_cooldown_seconds(
                    profile_snapshot,
                    elapsed_seconds=time.monotonic() - slice_started,
                    workload=workload,
                )
            )
            self._active_profile_snapshot = {}
            if delay > 0.0:
                time.sleep(delay)

    def maintain_heartbeats(self, job):
        """Refresh RQ heartbeats and sample this worker's cgroup mid-job.

        RQ calls this from the parent while it monitors a long-running
        workhorse.  Without this hook, ``memory.events`` was sampled only at
        dequeue boundaries, so a week-long adaptive coordinator could hit
        ``memory.max`` and continue admitting child slices without promoting
        the shared critical latch.
        """

        result = super().maintain_heartbeats(job)
        try:
            snapshot = get_resource_pressure_snapshot_sync(
                redis_client=self.connection
            )
        except Exception:
            snapshot = {
                "status": "paused",
                "controller_mode": "critical",
                "reasons": ["redis_unavailable"],
            }
        try:
            self._publish_pressure_state(snapshot)
        except Exception:
            # The RQ heartbeat is authoritative for job liveness.  Keep it
            # running and fail closed at the next admission boundary if the
            # optional telemetry publish itself is temporarily unavailable.
            self.log.exception(
                "Unable to sample worker cgroup during workhorse heartbeat"
            )
        return result

    def dequeue_job_and_maintain_ttl(self, timeout, max_idle_time=None):
        interval = max(1.0, settings.resource_pressure_sample_interval_seconds)
        if self._uses_heavy_gate():
            return self._dequeue_heavy_job(timeout, max_idle_time, interval)

        # Control/default/scheduled queues remain available during critical
        # pressure.  Heavy jobs routed through them are independently guarded
        # by the heavy_io decorators before workload start.
        return super().dequeue_job_and_maintain_ttl(timeout, max_idle_time)
