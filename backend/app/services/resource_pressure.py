"""Host pressure sampling and shared admission state.

The service deliberately reads Linux ``/proc`` rather than the Docker socket.
This keeps the backend unprivileged while still observing the host-wide memory,
swap and pressure-stall signals exposed to the containers on the NAS.

The state machine is independent from I/O and time so its hysteresis and
fail-closed behaviour can be unit tested without a running Redis instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import settings
from app.services.queue_admission import QUEUE_REJECTION_COUNTER_KEY
from app.services.resource_pressure_state import (
    GIB,
    MIB,
    PRESSURE_BASELINE_WINDOW_SECONDS,
    PRESSURE_BASELINE_SAMPLE_SECONDS,
    PRESSURE_BASELINE_MIN_SAMPLES,
    RESOURCE_PROFILES,
    RESOURCE_PROFILE_ALIASES,
    PressureThresholds,
    ResourceProfile,
    ResourceSliceLimits,
    ResourcePressureStateMachine,
    _configured_enforced_profiles,
    _detected_memory_total_bytes,
    automatic_memory_reserve_bytes,
    profile_soft_budget_enforced,
    thresholds_from_settings,
    workload_profile_name,
)
from app.services.resource_pressure_sampling import (
    FOREGROUND_LATENCY_WINDOW_SECONDS,
    LOCAL_CGROUP_WARNING_SECONDS,
    ResourceSample,
    _parse_meminfo,
    _parse_psi_full,
    _parse_psi_full_avg10,
    _parse_vmstat_swap_pages,
    _sample_cgroup_event_deltas,
    _sample_trend_rates,
    current_memory_available_bytes,
    foreground_latency_snapshot,
    record_foreground_latency,
    sample_cgroup_contribution,
    sample_cgroup_memory_events,
    sample_resource_metrics,
)
from app.services.resource_pressure_snapshot import (
    PRESSURE_SNAPSHOT_KEY,
    RESOURCE_CONTROL_CHANNEL,
    _redis_client,
    publish_resource_pressure_snapshot,
    read_shared_resource_pressure_snapshot,
)

logger = logging.getLogger(__name__)

PRESSURE_LATCH_KEY = "resource:pressure:latch"
CGROUP_OOM_ACK_HASH_KEY = "resource:cgroup:oom-kill-ack:v1"
CGROUP_OOM_ACK_SEEN_KEY = "resource:cgroup:oom-kill-seen:v1"
CGROUP_OOM_RECOVERED_HASH_KEY = "resource:cgroup:oom-kill-recovered:v1"
CGROUP_OOM_EVENT_ID_HASH_KEY = "resource:cgroup:oom-kill-event-id:v1"
PRESSURE_LATCH_TTL_SECONDS = 24 * 60 * 60
CGROUP_OOM_ACK_RETENTION_SECONDS = 2 * PRESSURE_LATCH_TTL_SECONDS
CGROUP_OOM_ACK_TOUCH_INTERVAL_SECONDS = 60 * 60
PRESSURE_BASELINE_KEY = "resource:pressure:baseline:v1"
PRESSURE_BASELINE_PERSIST_SECONDS = 5 * 60.0
WORKER_SUPERVISOR_PREFIX = "worker:supervisor:"
WORKER_SUPERVISOR_HASH_KEY = "worker:supervisors:v1"

_CGROUP_OOM_LATCH_ACK_LUA = """
local now = tonumber(ARGV[5])
local retention = tonumber(ARGV[6])
local candidate_ok, candidate_latch = pcall(cjson.decode, ARGV[3])
local candidate_controller = candidate_ok
    and type(candidate_latch) == 'table'
    and candidate_latch['controller']
    or nil
local candidate_event = type(candidate_controller) == 'table'
    and candidate_controller['external_event_id']
    or nil
if redis.call('EXISTS', KEYS[3]) == 0 and redis.call('EXISTS', KEYS[1]) == 1 then
    local legacy_ids = redis.call('HKEYS', KEYS[1])
    for _, legacy_id in ipairs(legacy_ids) do
        redis.call('ZADD', KEYS[3], now, legacy_id)
    end
    redis.call('EXPIRE', KEYS[1], retention)
    redis.call('EXPIRE', KEYS[3], retention)
end
local acknowledged = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0')
local candidate = tonumber(ARGV[2])
if acknowledged >= candidate then
    redis.call('ZADD', KEYS[3], now, ARGV[1])
    local expired_ids = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now - retention)
    for _, expired_id in ipairs(expired_ids) do
        redis.call('HDEL', KEYS[1], expired_id)
        redis.call('HDEL', KEYS[4], expired_id)
        redis.call('HDEL', KEYS[5], expired_id)
        redis.call('ZREM', KEYS[3], expired_id)
    end
    redis.call('EXPIRE', KEYS[1], retention)
    redis.call('EXPIRE', KEYS[3], retention)
    redis.call('EXPIRE', KEYS[4], retention)
    redis.call('EXPIRE', KEYS[5], retention)
    local recovered = tonumber(redis.call('HGET', KEYS[4], ARGV[1]) or '0')
    if recovered >= candidate then
        return 2
    end
    local acknowledged_event = redis.call('HGET', KEYS[5], ARGV[1])
    if not acknowledged_event
        and acknowledged == candidate
        and type(candidate_controller) == 'table'
        and candidate_controller['external_cgroup_id'] == ARGV[1]
        and tonumber(candidate_controller['external_oom_kill_counter']) == candidate
        and type(candidate_event) == 'string'
        and candidate_event ~= ''
    then
        redis.call('HSET', KEYS[5], ARGV[1], candidate_event)
        redis.call('EXPIRE', KEYS[5], retention)
        acknowledged_event = candidate_event
    end
    local raw_latch = redis.call('GET', KEYS[2])
    if raw_latch then
        local ok, latch = pcall(cjson.decode, raw_latch)
        local controller = ok and type(latch) == 'table' and latch['controller'] or nil
        local current_cgroup = type(controller) == 'table'
            and controller['external_cgroup_id']
            or nil
        local current_counter = type(controller) == 'table'
            and tonumber(controller['external_oom_kill_counter'])
            or nil
        local current_ack = type(current_cgroup) == 'string'
            and tonumber(redis.call('HGET', KEYS[1], current_cgroup) or '0')
            or 0
        local current_recovered = type(current_cgroup) == 'string'
            and tonumber(redis.call('HGET', KEYS[4], current_cgroup) or '0')
            or 0
        local current_event = type(current_cgroup) == 'string'
            and redis.call('HGET', KEYS[5], current_cgroup)
            or nil
        local adoptable_counter = tonumber(ARGV[9])
        local legacy_event_missing = not current_event
            and ok
            and type(latch) == 'table'
            and latch['status'] == 'paused'
            and type(controller) == 'table'
            and type(controller['external_event_id']) == 'string'
            and controller['external_event_id'] ~= ''
            and type(current_cgroup) == 'string'
            and current_cgroup ~= ''
            and current_counter ~= nil
            and current_counter > 0
            and current_ack == current_counter
            and current_recovered < current_counter
        if not current_event
            and legacy_event_missing
            and type(controller) == 'table'
            and controller['external_event_id'] == ARGV[7]
            and current_cgroup == ARGV[8]
            and current_counter == adoptable_counter
            and current_ack == current_counter
            and current_recovered < current_counter
        then
            redis.call('HSET', KEYS[5], current_cgroup, controller['external_event_id'])
            redis.call('EXPIRE', KEYS[5], retention)
            current_event = controller['external_event_id']
        end
        if legacy_event_missing and not current_event and tonumber(ARGV[10]) == 0 then
            return 3
        end
        local represents_active_ack = type(controller) == 'table'
            and type(controller['external_event_id']) == 'string'
            and controller['external_event_id'] ~= ''
            and type(current_cgroup) == 'string'
            and current_cgroup ~= ''
            and current_counter ~= nil
            and current_counter > 0
            and current_ack == current_counter
            and current_recovered < current_counter
            and current_event == controller['external_event_id']
        if ok
            and type(latch) == 'table'
            and latch['status'] == 'paused'
            and represents_active_ack
        then
            return 0
        end
    end
    local repaired_payload = ARGV[3]
    if acknowledged_event
        and candidate_ok
        and type(candidate_latch) == 'table'
        and type(candidate_controller) == 'table'
    then
        candidate_controller['external_event_id'] = acknowledged_event
        candidate_controller['external_cgroup_id'] = ARGV[1]
        candidate_controller['external_oom_kill_counter'] = acknowledged
        repaired_payload = cjson.encode(candidate_latch)
    end
    redis.call('SET', KEYS[2], repaired_payload, 'EX', ARGV[4])
    return 1
end
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
if type(candidate_event) == 'string' and candidate_event ~= '' then
    redis.call('HSET', KEYS[5], ARGV[1], candidate_event)
end
redis.call('ZADD', KEYS[3], now, ARGV[1])
local expired_ids = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now - retention)
for _, expired_id in ipairs(expired_ids) do
    redis.call('HDEL', KEYS[1], expired_id)
    redis.call('HDEL', KEYS[4], expired_id)
    redis.call('HDEL', KEYS[5], expired_id)
    redis.call('ZREM', KEYS[3], expired_id)
end
redis.call('EXPIRE', KEYS[1], retention)
redis.call('EXPIRE', KEYS[3], retention)
redis.call('EXPIRE', KEYS[4], retention)
redis.call('EXPIRE', KEYS[5], retention)
return 1
"""

_CGROUP_OOM_ACK_TOUCH_LUA = """
local acknowledged = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '0')
local candidate = tonumber(ARGV[2])
if acknowledged < candidate then
    return 0
end
local now = tonumber(ARGV[3])
local retention = tonumber(ARGV[4])
redis.call('ZADD', KEYS[2], now, ARGV[1])
local expired_ids = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now - retention)
for _, expired_id in ipairs(expired_ids) do
    redis.call('HDEL', KEYS[1], expired_id)
    redis.call('HDEL', KEYS[3], expired_id)
    redis.call('HDEL', KEYS[4], expired_id)
    redis.call('ZREM', KEYS[2], expired_id)
end
redis.call('EXPIRE', KEYS[1], retention)
redis.call('EXPIRE', KEYS[2], retention)
redis.call('EXPIRE', KEYS[3], retention)
redis.call('EXPIRE', KEYS[4], retention)
local recovered = tonumber(redis.call('HGET', KEYS[3], ARGV[1]) or '0')
if recovered >= candidate then
    return 2
end
return 1
"""

_CLEAR_PRESSURE_LATCH_LUA = """
local raw_latch = redis.call('GET', KEYS[1])
if not raw_latch then
    return 0
end
local ok, latch = pcall(cjson.decode, raw_latch)
if not ok or type(latch) ~= 'table' or latch['status'] ~= 'paused' then
    return 0
end
local controller = latch['controller'] or {}
local current_event = tostring(controller['external_event_id'] or '')
if current_event ~= ARGV[1] then
    return 0
end
if current_event ~= '' then
    local current_cgroup = controller['external_cgroup_id']
    local current_counter = tonumber(controller['external_oom_kill_counter'])
    if type(current_cgroup) ~= 'string' or current_cgroup == '' or not current_counter then
        return 0
    end
    local acknowledged = tonumber(redis.call('HGET', KEYS[2], current_cgroup) or '0')
    local recovered = tonumber(redis.call('HGET', KEYS[3], current_cgroup) or '0')
    local acknowledged_event = redis.call('HGET', KEYS[5], current_cgroup)
    local adoptable_counter = tonumber(ARGV[6])
    if not acknowledged_event
        and current_event == ARGV[4]
        and current_cgroup == ARGV[5]
        and current_counter == adoptable_counter
        and acknowledged == current_counter
        and recovered < current_counter
    then
        redis.call('HSET', KEYS[5], current_cgroup, current_event)
        redis.call('EXPIRE', KEYS[5], ARGV[2])
        acknowledged_event = current_event
    end
    if acknowledged ~= current_counter
        or recovered >= current_counter
        or acknowledged_event ~= current_event
    then
        return 0
    end
    local acknowledgments = redis.call('HGETALL', KEYS[2])
    for index = 1, #acknowledgments, 2 do
        redis.call('HSET', KEYS[3], acknowledgments[index], acknowledgments[index + 1])
    end
    if #acknowledgments > 0 then
        redis.call('EXPIRE', KEYS[2], ARGV[2])
        redis.call('EXPIRE', KEYS[3], ARGV[2])
        redis.call('EXPIRE', KEYS[4], ARGV[2])
        redis.call('EXPIRE', KEYS[5], ARGV[2])
    end
end
redis.call('DEL', KEYS[1])
return 1
"""


def cgroup_oom_kill_event_id(cgroup_id: str, oom_kill_counter: int) -> str:
    """Return one stable identity for a cumulative cgroup OOM-kill event."""

    identity = str(cgroup_id or "unknown")
    counter = max(0, int(oom_kill_counter))
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"auto-gallery:cgroup-oom-kill:{identity}:{counter}",
    ).hex


def _adoptable_legacy_cgroup_oom_latch_identity(
    snapshot: dict[str, Any] | None,
) -> tuple[str, str, int] | None:
    controller = (snapshot or {}).get("controller") or {}
    if not isinstance(controller, dict):
        return None
    cgroup_id = controller.get("external_cgroup_id")
    event_id = controller.get("external_event_id")
    try:
        counter = int(controller.get("external_oom_kill_counter"))
    except (TypeError, ValueError):
        return None
    if not isinstance(cgroup_id, str) or not cgroup_id or counter <= 0:
        return None
    expected_event_id = cgroup_oom_kill_event_id(cgroup_id, counter)
    if not isinstance(event_id, str) or event_id != expected_event_id:
        return None
    return event_id, cgroup_id, counter


DEFAULT_QUEUE_NAMES = (
    "default",
    "downloads",
    "imports",
    "operations",
    "maintenance",
    "scheduled",
    "downloads:pixiv",
    "downloads:danbooru",
    "downloads:iwara",
    "downloads:weibo",
    "downloads:bilibili",
    "downloads:pinterest",
    "downloads:lofter",
    "downloads:x",
)


class PressureBaselineWindow:
    """Idle-only, bounded PSI baseline used to separate NAS work from ours.

    Host PSI includes UGREEN media indexing, SMB/NFS and unrelated containers,
    so a fixed threshold alone can leave auto-gallery permanently constrained.
    The baseline is learned only while no auto-gallery resource lease is active.
    Values above a fixed safety cap are deliberately not learned, which prevents
    a pathological host from teaching the controller that severe pressure is
    normal.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        # One observation per minute for 24 hours, with a little clock-skew
        # headroom.  The time-window prune remains authoritative.
        self._samples: deque[tuple[float, float | None, float | None]] = deque(
            maxlen=PRESSURE_BASELINE_WINDOW_SECONDS // 60 + 16
        )
        self._hydrated = False
        self._dirty = False
        self._last_persisted_at = 0.0
        self._last_observed_at = 0.0

    @property
    def hydrated(self) -> bool:
        return self._hydrated

    def _prune(self, now: float) -> None:
        cutoff = now - PRESSURE_BASELINE_WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
            self._dirty = True

    def hydrate(self, raw: Any) -> None:
        """Hydrate once from a compact Redis payload; malformed data is ignored."""

        if self._hydrated:
            return
        now = self.clock()
        payload: dict[str, Any] = {}
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw:
                decoded = json.loads(str(raw))
                if isinstance(decoded, dict):
                    payload = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring malformed resource pressure baseline")

        cutoff = now - PRESSURE_BASELINE_WINDOW_SECONDS
        for value in payload.get("samples") or []:
            try:
                sampled_at = float(value[0])
                memory_psi = None if value[1] is None else float(value[1])
                io_psi = None if value[2] is None else float(value[2])
            except (IndexError, TypeError, ValueError):
                continue
            if sampled_at < cutoff or sampled_at > now + 300:
                continue
            if memory_psi is None and io_psi is None:
                continue
            self._samples.append((sampled_at, memory_psi, io_psi))
        self._samples = deque(
            sorted(self._samples, key=lambda value: value[0]),
            maxlen=PRESSURE_BASELINE_WINDOW_SECONDS // 60 + 16,
        )
        if self._samples:
            self._last_observed_at = self._samples[-1][0]
        self._last_persisted_at = now
        self._dirty = False
        self._hydrated = True

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        # Nearest-rank is stable for the small rolling sample and avoids
        # implying precision the kernel's PSI averages do not have.
        rank = max(1, int((percentile * len(ordered)) + 0.999999))
        return round(ordered[min(len(ordered) - 1, rank - 1)], 4)

    def _statistics(self, now: float) -> dict[str, Any]:
        self._prune(now)
        memory_values = [value for _, value, _ in self._samples if value is not None]
        io_values = [value for _, _, value in self._samples if value is not None]
        sample_count = max(len(memory_values), len(io_values))
        return {
            "memory_median": self._percentile(memory_values, 0.50),
            "memory_p95": self._percentile(memory_values, 0.95),
            "io_median": self._percentile(io_values, 0.50),
            "io_p95": self._percentile(io_values, 0.95),
            "memory_sample_count": len(memory_values),
            "io_sample_count": len(io_values),
            "sample_count": sample_count,
            "ready": sample_count >= PRESSURE_BASELINE_MIN_SAMPLES,
        }

    def enrich(
        self,
        sample: ResourceSample,
        *,
        heavy_idle: bool,
        thresholds: PressureThresholds,
    ) -> ResourceSample:
        now = self.clock()
        self._prune(now)
        observed = False
        observation_due = now - self._last_observed_at >= PRESSURE_BASELINE_SAMPLE_SECONDS
        swap_hazard = (
            sample.swap_free_ratio < thresholds.pause_swap_free_ratio
            and (sample.swap_activity_bytes_per_second or 0.0)
            >= thresholds.swap_activity_bytes_per_second
            and (sample.memory_available_change_bytes_per_second or 0.0) < 0
        )
        safe_host = (
            sample.memory_available_bytes >= thresholds.pause_available_bytes
            and sample.swap_free_ratio >= thresholds.critical_swap_free_ratio
            and not swap_hazard
            and not (sample.cgroup_memory_oom_kill_delta or 0)
        )
        if heavy_idle and observation_due and safe_host:
            memory_cap = max(
                thresholds.pause_memory_psi,
                float(settings.resource_baseline_memory_psi_cap),
            )
            io_cap = max(
                thresholds.pause_io_psi,
                float(settings.resource_baseline_io_psi_cap),
            )
            memory_value = (
                float(sample.memory_full_avg10)
                if sample.memory_full_avg10 is not None
                and 0.0 <= sample.memory_full_avg10 <= memory_cap
                else None
            )
            io_value = (
                float(sample.io_full_avg10)
                if sample.io_full_avg10 is not None
                and 0.0 <= sample.io_full_avg10 <= io_cap
                else None
            )
            if memory_value is not None or io_value is not None:
                self._samples.append((now, memory_value, io_value))
                self._last_observed_at = now
                self._dirty = True
                observed = True

        statistics = self._statistics(now)
        memory_trigger = thresholds.pause_memory_psi
        io_trigger = thresholds.pause_io_psi
        if statistics["ready"]:
            if (
                statistics["memory_sample_count"] >= PRESSURE_BASELINE_MIN_SAMPLES
                and statistics["memory_p95"] is not None
            ):
                memory_trigger = min(
                    max(
                        thresholds.pause_memory_psi,
                        float(statistics["memory_p95"])
                        + float(settings.resource_baseline_memory_psi_margin),
                    ),
                    max(
                        thresholds.pause_memory_psi,
                        float(settings.resource_baseline_memory_psi_cap),
                    ),
                )
            if (
                statistics["io_sample_count"] >= PRESSURE_BASELINE_MIN_SAMPLES
                and statistics["io_p95"] is not None
            ):
                io_trigger = min(
                    max(
                        thresholds.pause_io_psi,
                        float(statistics["io_p95"])
                        + float(settings.resource_baseline_io_psi_margin),
                    ),
                    max(
                        thresholds.pause_io_psi,
                        float(settings.resource_baseline_io_psi_cap),
                    ),
                )

        return replace(
            sample,
            baseline_memory_psi_median=statistics["memory_median"],
            baseline_memory_psi_p95=statistics["memory_p95"],
            baseline_io_psi_median=statistics["io_median"],
            baseline_io_psi_p95=statistics["io_p95"],
            baseline_sample_count=int(statistics["sample_count"]),
            baseline_idle_observation=observed,
            memory_psi_soft_trigger=round(memory_trigger, 4),
            io_psi_soft_trigger=round(io_trigger, 4),
        )

    def persistence_payload(self) -> dict[str, Any] | None:
        now = self.clock()
        self._prune(now)
        if not self._dirty or now - self._last_persisted_at < PRESSURE_BASELINE_PERSIST_SECONDS:
            return None
        return {
            "version": 1,
            "window_seconds": PRESSURE_BASELINE_WINDOW_SECONDS,
            "idle_only": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "samples": list(self._samples),
        }

    def mark_persisted(self) -> None:
        self._last_persisted_at = self.clock()
        self._dirty = False


def resource_profile_permit(
    snapshot: dict[str, Any],
    workload: str | None,
) -> tuple[bool, dict[str, Any]]:
    """Return the rolling-upgrade-safe permit for one workload profile."""

    profile_name = workload_profile_name(workload)
    budget = snapshot.get("budget") if isinstance(snapshot, dict) else None
    profiles = budget.get("profiles") if isinstance(budget, dict) else None
    profile = profiles.get(profile_name) if isinstance(profiles, dict) else None
    if isinstance(profile, dict) and "allowed" in profile:
        details = {
            **profile,
            "profile": profile_name,
            "controller_mode": snapshot.get("controller_mode") or (
                "critical" if snapshot.get("status") == "paused" else "constrained"
                if snapshot.get("status") == "warning" else "normal"
            ),
            "throughput_scale": budget.get("throughput_scale"),
            "computed_throughput_scale": budget.get("computed_throughput_scale"),
            "effective_throughput_scale": budget.get("effective_throughput_scale"),
            "generation": budget.get("generation"),
            "governance_mode": budget.get("governance_mode", "enforce"),
        }
        return bool(profile["allowed"]), details

    # Old snapshots know only the binary pause contract.  Fail closed on an
    # explicit pause, otherwise permit the workload until a new monitor sample
    # publishes profile budgets.
    allowed = snapshot.get("status") != "paused"
    return allowed, {
        "allowed": allowed,
        "profile": profile_name,
        "controller_mode": "critical" if not allowed else "normal",
        "reason": None if allowed else "legacy_pressure_pause",
        "compatibility_fallback": True,
    }


def profile_slice_limits(
    snapshot: dict[str, Any],
    workload: str | None,
    *,
    max_work_units: int | None = None,
    max_slice_seconds: float | None = None,
) -> ResourceSliceLimits:
    """Resolve hard admission and soft AIMD limits for one cooperative slice.

    The helper is deliberately pure so callers can evaluate it *before*
    opening a transaction or acquiring a filesystem/Redis lease.  Shadow mode
    returns the profile's base limits while still honoring every hard gate.
    Enforce mode consumes the published effective budget and never returns a
    zero-unit permitted slice, preserving constrained forward progress.
    """

    snapshot = snapshot if isinstance(snapshot, dict) else {}
    profile_name = workload_profile_name(workload)
    base = RESOURCE_PROFILES[profile_name]
    permitted, permit = resource_profile_permit(snapshot, workload)
    budget = snapshot.get("budget") or {}
    controller_mode = str(
        snapshot.get("controller_mode")
        or permit.get("controller_mode")
        or ("critical" if snapshot.get("status") == "paused" else "normal")
    ).lower()
    governance_mode = str(
        budget.get("governance_mode")
        or permit.get("governance_mode")
        or "enforce"
    ).lower()
    governance_mode = "shadow" if governance_mode == "shadow" else "enforce"

    # Treat an inconsistent critical/paused payload as denied even if its
    # embedded profile was stale and still said allowed=true.
    allowed = bool(permitted) and controller_mode != "critical" and snapshot.get(
        "status"
    ) != "paused"
    try:
        effective_scale = float(
            budget.get(
                "effective_throughput_scale",
                budget.get("throughput_scale", 1.0),
            )
        )
    except (TypeError, ValueError):
        effective_scale = 1.0
    effective_scale = max(0.0, min(1.0, effective_scale))
    if not allowed:
        return ResourceSliceLimits(
            profile=profile_name,
            allowed=False,
            work_units=0,
            slice_seconds=0.0,
            governance_mode=governance_mode,
            controller_mode=controller_mode,
            effective_scale=0.0 if controller_mode == "critical" else effective_scale,
            reason=str(permit.get("reason") or "resource_pressure"),
        )

    configured_unit_cap = max(1, int(base.base_work_units))
    if max_work_units is not None:
        configured_unit_cap = min(
            configured_unit_cap,
            max(1, int(max_work_units)),
        )

    configured_seconds_cap = base.base_slice_seconds
    if max_slice_seconds is not None:
        caller_seconds_cap = max(0.001, float(max_slice_seconds))
        configured_seconds_cap = (
            caller_seconds_cap
            if configured_seconds_cap is None
            else min(configured_seconds_cap, caller_seconds_cap)
        )

    profile_payload = ((budget.get("profiles") or {}).get(profile_name) or {})
    soft_enforced = bool(profile_payload.get("soft_budget_enforced", governance_mode == "enforce"))
    if not soft_enforced:
        governance_mode = "shadow"
        units = configured_unit_cap
        seconds = configured_seconds_cap
        effective_scale = 1.0
    else:
        minimum_scale = max(
            0.01,
            min(1.0, float(settings.resource_budget_min_scale)),
        )
        effective_scale = max(minimum_scale, effective_scale)
        try:
            published_units = int(profile_payload.get("work_units"))
        except (TypeError, ValueError):
            published_units = int(base.base_work_units * effective_scale)
        units = min(configured_unit_cap, max(1, published_units))

        if configured_seconds_cap is None:
            seconds = None
        else:
            try:
                published_seconds = float(profile_payload.get("slice_seconds"))
            except (TypeError, ValueError):
                published_seconds = configured_seconds_cap * effective_scale
            seconds = min(configured_seconds_cap, max(0.001, published_seconds))

    return ResourceSliceLimits(
        profile=profile_name,
        allowed=True,
        work_units=units,
        slice_seconds=seconds,
        governance_mode=governance_mode,
        controller_mode=controller_mode,
        effective_scale=effective_scale,
        reason=None,
    )


def profile_slice_cooldown_seconds(
    snapshot: dict[str, Any],
    *,
    elapsed_seconds: float,
    max_seconds: float = 30.0,
    jitter: bool = True,
    workload: str | None = None,
) -> float:
    """Return enforce-only lock-free idle time for an AIMD work slice.

    Batch shrinking bounds peak cost but cannot reduce sustained throughput
    when backlog is permanent.  The duty-cycle relation
    ``active / (active + idle) == effective_scale`` supplies the missing rate
    control.  The delay is capped so constrained work keeps making progress;
    critical mode remains the hard admission gate and is never implemented as
    a very long sleep.
    """

    snapshot = snapshot if isinstance(snapshot, dict) else {}
    budget = snapshot.get("budget") or {}
    governance_mode = str(budget.get("governance_mode") or "enforce").lower()
    controller_mode = str(
        snapshot.get("controller_mode")
        or ("critical" if snapshot.get("status") == "paused" else "normal")
    ).lower()
    if workload is not None:
        profile_name = workload_profile_name(workload)
        profile_payload = ((budget.get("profiles") or {}).get(profile_name) or {})
        if not bool(profile_payload.get("soft_budget_enforced", governance_mode == "enforce")):
            governance_mode = "shadow"
    if governance_mode != "enforce" or controller_mode == "critical" or snapshot.get(
        "status"
    ) == "paused":
        return 0.0
    try:
        elapsed = max(0.0, float(elapsed_seconds))
        scale = float(
            budget.get(
                "effective_throughput_scale",
                budget.get("throughput_scale", 1.0),
            )
        )
        cap = max(0.0, float(max_seconds))
    except (TypeError, ValueError):
        return 0.0
    if elapsed <= 0.0 or cap <= 0.0 or scale >= 1.0:
        return 0.0
    minimum_scale = max(
        0.01,
        min(1.0, float(settings.resource_budget_min_scale)),
    )
    scale = max(minimum_scale, min(1.0, scale))
    delay = elapsed * ((1.0 / scale) - 1.0)
    if jitter and delay > 0.0:
        delay *= random.uniform(0.85, 1.15)
    return round(min(cap, max(0.0, delay)), 3)


async def sleep_for_profile_slice_cooldown(
    snapshot: dict[str, Any],
    *,
    elapsed_seconds: float,
    max_seconds: float = 30.0,
    workload: str | None = None,
) -> float:
    """Sleep after releasing a slice's transaction and leases, then return it."""

    delay = profile_slice_cooldown_seconds(
        snapshot,
        elapsed_seconds=elapsed_seconds,
        max_seconds=max_seconds,
        workload=workload,
    )
    if delay > 0.0:
        await asyncio.sleep(delay)
    return delay


class ResourcePressureMonitor:
    def __init__(
        self,
        state_machine: ResourcePressureStateMachine | None = None,
        *,
        sampler: Callable[[], ResourceSample] = sample_resource_metrics,
        baseline: PressureBaselineWindow | None = None,
    ) -> None:
        self.state_machine = state_machine or ResourcePressureStateMachine()
        self.sampler = sampler
        self.baseline = baseline or PressureBaselineWindow()
        self._lock = threading.Lock()
        self._hydrated = False
        self._baseline_hydrate_attempted_at = 0.0
        self._heavy_idle = False
        self._last_external_pause_marker: tuple[Any, ...] | None = None

    def hydrate_baseline_once(self, redis_client=None) -> None:
        """Load the 24-hour idle baseline, retrying Redis at most once/minute."""

        with self._lock:
            if self.baseline.hydrated:
                return
            now = time.monotonic()
            if now - self._baseline_hydrate_attempted_at < 60.0:
                return
            self._baseline_hydrate_attempted_at = now
        try:
            raw = _redis_client(redis_client).get(PRESSURE_BASELINE_KEY)
        except Exception:
            logger.debug("Unable to hydrate resource pressure baseline", exc_info=True)
            return
        with self._lock:
            self.baseline.hydrate(raw)

    def set_heavy_idle(self, value: bool) -> None:
        with self._lock:
            self._heavy_idle = bool(value)

    def persist_baseline_if_due(self, redis_client=None) -> None:
        """Persist at most every five minutes, keeping idle Redis writes tiny."""

        with self._lock:
            payload = self.baseline.persistence_payload()
        if payload is None:
            return
        try:
            _redis_client(redis_client).set(
                PRESSURE_BASELINE_KEY,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
                ex=2 * PRESSURE_BASELINE_WINDOW_SECONDS,
            )
        except Exception:
            logger.debug("Unable to persist resource pressure baseline", exc_info=True)
            return
        with self._lock:
            self.baseline.mark_persisted()

    @staticmethod
    def _external_pause_marker(snapshot: dict[str, Any]) -> tuple[Any, ...] | None:
        controller = snapshot.get("controller") or {}
        source = controller.get("external_source")
        reasons = tuple(snapshot.get("trigger_reasons") or snapshot.get("reasons") or [])
        if not source:
            return None
        event_id = controller.get("external_event_id")
        if event_id:
            return (source, str(event_id))
        return (source, snapshot.get("sampled_at"), reasons)

    def hydrate_paused_once(self, snapshot: dict[str, Any] | None) -> None:
        """Hydrate once per process before the first local sample."""

        with self._lock:
            if self._hydrated:
                return
            self._hydrated = True
            if snapshot and snapshot.get("status") == "paused":
                self.state_machine.restore_paused(
                    snapshot.get("trigger_reasons") or snapshot.get("reasons") or None
                )
                self._last_external_pause_marker = self._external_pause_marker(snapshot)

    def enforce_external_pause(self, snapshot: dict[str, Any]) -> None:
        """Apply a worker-originated hard latch to an already-hydrated monitor."""

        with self._lock:
            self._hydrated = True
            marker = self._external_pause_marker(snapshot)
            if self.state_machine.status != "paused" or (
                marker is not None and marker != self._last_external_pause_marker
            ):
                self.state_machine.restore_paused(
                    snapshot.get("trigger_reasons") or snapshot.get("reasons") or None
                )
            if marker is not None:
                self._last_external_pause_marker = marker

    def sample_with_previous_status(self) -> tuple[str, dict[str, Any]]:
        with self._lock:
            self._hydrated = True
            previous_status = self.state_machine.status
            try:
                sample = self.sampler()
            except Exception as exc:  # noqa: BLE001 - fail-closed sampler boundary
                logger.warning("Resource pressure sampling failed: %s", exc)
                snapshot = self.state_machine.update(None, error=type(exc).__name__)
            else:
                sample = self.baseline.enrich(
                    sample,
                    heavy_idle=self._heavy_idle,
                    thresholds=self.state_machine.thresholds,
                )
                snapshot = self.state_machine.update(sample)
            return previous_status, snapshot

    def sample(self) -> dict[str, Any]:
        return self.sample_with_previous_status()[1]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.state_machine.snapshot()


_monitor = ResourcePressureMonitor()
def publish_external_resource_critical(
    reason: str,
    *,
    redis_client=None,
    source: str = "worker_cgroup",
    event_id: str | None = None,
    cgroup_id: str | None = None,
    oom_kill_counter: int | None = None,
) -> dict[str, Any] | None:
    """Merge a worker-local OOM kill into the shared hard safety latch."""

    if reason != "worker_cgroup_oom_kill":
        raise ValueError("only worker_cgroup_oom_kill may be promoted as resource critical")

    client = _redis_client(redis_client)
    tracked_counter = (
        max(0, int(oom_kill_counter))
        if cgroup_id is not None and oom_kill_counter is not None
        else None
    )
    if tracked_counter is not None:
        event_id = cgroup_oom_kill_event_id(cgroup_id, tracked_counter)

    shared = read_shared_resource_pressure_snapshot(redis_client=client)
    latch = read_resource_pressure_latch(redis_client=client)
    adoptable_latch_identity = _adoptable_legacy_cgroup_oom_latch_identity(latch)
    snapshot = deepcopy(shared or latch or {})
    if latch is not None:
        snapshot["reasons"] = list(
            dict.fromkeys(
                [*(latch.get("reasons") or []), *(snapshot.get("reasons") or [])]
            )
        )
        snapshot["trigger_reasons"] = list(
            dict.fromkeys(
                [
                    *(latch.get("trigger_reasons") or []),
                    *(snapshot.get("trigger_reasons") or []),
                ]
            )
        )
    snapshot["status"] = "paused"
    snapshot["controller_mode"] = "critical"
    snapshot["sampled_at"] = datetime.now(timezone.utc).isoformat()
    snapshot["reasons"] = list(
        dict.fromkeys([*(snapshot.get("reasons") or []), reason])
    )
    snapshot["hard_reasons"] = list(
        dict.fromkeys([*(snapshot.get("hard_reasons") or []), reason])
    )
    snapshot["trigger_reasons"] = list(
        dict.fromkeys([*(snapshot.get("trigger_reasons") or []), reason])
    )
    snapshot["recovery_remaining_seconds"] = 0.0
    snapshot.setdefault("soft_reasons", [])
    controller = snapshot.setdefault("controller", {})
    controller.update(
        mode="critical",
        legacy_status="paused",
        hard_gate_active=True,
        external_source=source,
        throughput_scale=0.0,
        computed_throughput_scale=0.0,
        effective_throughput_scale=0.0,
        external_event_id=event_id or uuid.uuid4().hex,
        external_cgroup_id=cgroup_id,
        external_oom_kill_counter=tracked_counter,
    )
    budget = snapshot.setdefault("budget", {})
    try:
        generation = int(budget.get("generation") or 0) + 1
    except (TypeError, ValueError):
        generation = 1
    budget["generation"] = generation
    controller["generation"] = generation
    budget["computed_throughput_scale"] = 0.0
    budget["effective_throughput_scale"] = 0.0
    budget["throughput_scale"] = 0.0
    budget["read_bytes_per_second"] = 0
    budget["write_bytes_per_second"] = 0
    for profile in (budget.get("profiles") or {}).values():
        if isinstance(profile, dict) and profile.get("memory_reservation_bytes", 0) > 0:
            profile.update(
                allowed=False,
                would_allow=False,
                reason=reason,
                grant="denied",
                hard_gate_enforced=True,
            )
    for grant in (budget.get("profile_grants") or {}).values():
        if isinstance(grant, dict) and grant.get("memory_reservation_bytes", 0) > 0:
            grant.update(allowed=False, grant="denied", reason=reason)
    if tracked_counter is not None:
        try:
            acknowledgment_state = _persist_cgroup_oom_latch_and_ack(
                client,
                str(cgroup_id),
                tracked_counter,
                snapshot,
                adoptable_latch_identity=adoptable_latch_identity,
            )
        except Exception as exc:
            raise RuntimeError("unable to persist worker cgroup OOM latch") from exc
        if acknowledgment_state == "recovered":
            return None
        if acknowledgment_state == "active":
            for existing in (
                read_resource_pressure_latch(redis_client=client),
                read_shared_resource_pressure_snapshot(redis_client=client),
            ):
                if existing and existing.get("status") == "paused":
                    return existing
            # A malformed/unreadable latch is never evidence of recovery.
            return snapshot
    elif not _refresh_pressure_latch(snapshot, redis_client=client):
        raise RuntimeError("unable to persist worker cgroup OOM latch")
    publish_resource_pressure_snapshot(snapshot, redis_client=client)
    return snapshot


def _pressure_latch_payload(
    snapshot: dict[str, Any],
    *,
    inherited_controller: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_controller = snapshot.get("controller") or {}
    external_controller: dict[str, Any] = {}
    for controller in (inherited_controller or {}, snapshot_controller):
        for key in (
            "external_source",
            "external_event_id",
            "external_cgroup_id",
            "external_oom_kill_counter",
        ):
            value = controller.get(key)
            if value is not None:
                external_controller[key] = value
    return {
        "status": "paused",
        "reasons": list(snapshot.get("reasons") or []),
        "trigger_reasons": list(snapshot.get("trigger_reasons") or []),
        "sampled_at": snapshot.get("sampled_at"),
        "controller": external_controller,
    }


def _persist_cgroup_oom_latch_and_ack(
    redis_client,
    cgroup_id: str,
    oom_kill_counter: int,
    snapshot: dict[str, Any],
    *,
    adoptable_latch_identity: tuple[str, str, int] | None = None,
) -> str:
    """Atomically advance one cgroup counter and its authoritative latch."""

    payload = json.dumps(
        _pressure_latch_payload(snapshot),
        separators=(",", ":"),
        ensure_ascii=True,
    )
    adoptable_event, adoptable_cgroup, adoptable_counter = (
        adoptable_latch_identity or ("", "", 0)
    )
    accepted = 3
    for adoption_checked in (0, 1):
        accepted = redis_client.eval(
            _CGROUP_OOM_LATCH_ACK_LUA,
            5,
            CGROUP_OOM_ACK_HASH_KEY,
            PRESSURE_LATCH_KEY,
            CGROUP_OOM_ACK_SEEN_KEY,
            CGROUP_OOM_RECOVERED_HASH_KEY,
            CGROUP_OOM_EVENT_ID_HASH_KEY,
            str(cgroup_id),
            max(0, int(oom_kill_counter)),
            payload,
            PRESSURE_LATCH_TTL_SECONDS,
            int(time.time()),
            CGROUP_OOM_ACK_RETENTION_SECONDS,
            adoptable_event,
            adoptable_cgroup,
            adoptable_counter,
            adoption_checked,
        )
        if int(accepted or 0) != 3:
            break
        adoptable_event, adoptable_cgroup, adoptable_counter = (
            _adoptable_legacy_cgroup_oom_latch_identity(
                read_resource_pressure_latch(redis_client=redis_client)
            )
            or ("", "", 0)
        )
    return {0: "active", 1: "promoted", 2: "recovered"}.get(
        int(accepted or 0),
        "active",
    )


def touch_cgroup_oom_kill_ack(
    redis_client,
    cgroup_id: str,
    oom_kill_counter: int,
) -> str:
    """Keep one live cgroup identity bounded without replaying its counter."""

    state = redis_client.eval(
        _CGROUP_OOM_ACK_TOUCH_LUA,
        4,
        CGROUP_OOM_ACK_HASH_KEY,
        CGROUP_OOM_ACK_SEEN_KEY,
        CGROUP_OOM_RECOVERED_HASH_KEY,
        CGROUP_OOM_EVENT_ID_HASH_KEY,
        str(cgroup_id),
        max(0, int(oom_kill_counter)),
        int(time.time()),
        CGROUP_OOM_ACK_RETENTION_SECONDS,
    )
    return {0: "missing", 1: "active", 2: "recovered"}.get(
        int(state or 0),
        "missing",
    )


def promote_worker_cgroup_oom_kills(
    contributions,
    *,
    redis_client=None,
) -> int:
    """Promote each newly observed worker-cgroup OOM counter at most once."""

    client = _redis_client(redis_client)
    latest_by_cgroup: dict[str, dict[str, Any]] = {}
    for contribution in contributions:
        if not isinstance(contribution, dict):
            continue
        memory_events = contribution.get("memory_events") or {}
        counter = max(0, int(memory_events.get("oom_kill") or 0))
        delta = max(0, int(memory_events.get("oom_kill_delta") or 0))
        if not counter or not delta:
            continue
        cgroup_id = str(contribution.get("cgroup_id") or "unknown")
        previous = latest_by_cgroup.get(cgroup_id)
        previous_events = (previous or {}).get("memory_events") or {}
        previous_counter = max(0, int(previous_events.get("oom_kill") or 0))
        if previous is None or counter > previous_counter or (
            counter == previous_counter
            and str(contribution.get("sampled_at") or "")
            > str(previous.get("sampled_at") or "")
        ):
            latest_by_cgroup[cgroup_id] = contribution

    promoted = 0
    for cgroup_id, contribution in sorted(latest_by_cgroup.items()):
        memory_events = contribution.get("memory_events") or {}
        counter = max(0, int(memory_events.get("oom_kill") or 0))
        try:
            snapshot = publish_external_resource_critical(
                "worker_cgroup_oom_kill",
                redis_client=client,
                source="worker_health_aggregate",
                event_id=cgroup_oom_kill_event_id(cgroup_id, counter),
                cgroup_id=cgroup_id,
                oom_kill_counter=counter,
            )
            if snapshot is not None:
                promoted += 1
        except Exception:
            logger.warning(
                "Unable to promote aggregated worker cgroup memory event",
                exc_info=True,
            )
    return promoted


def read_resource_pressure_latch(redis_client=None) -> dict[str, Any] | None:
    try:
        raw = _redis_client(redis_client).get(PRESSURE_LATCH_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("status") != "paused":
            return None
        value.setdefault("reasons", ["restored_pressure_latch"])
        return value
    except Exception:
        return None


def _refresh_pressure_latch(snapshot: dict[str, Any], redis_client=None) -> bool:
    """Persist paused state without adding a Redis/AOF write every 10 seconds."""

    try:
        client = _redis_client(redis_client)
        current = read_resource_pressure_latch(redis_client=client)
        ttl = client.ttl(PRESSURE_LATCH_KEY) if current else -2
        reasons_changed = bool(current) and current.get("reasons") != snapshot.get("reasons")
        current_controller = (current or {}).get("controller") or {}
        payload = _pressure_latch_payload(
            snapshot,
            inherited_controller=current_controller,
        )
        snapshot_controller = payload["controller"]
        external_event_changed = current_controller.get(
            "external_event_id"
        ) != snapshot_controller.get("external_event_id")
        if (
            current is None
            or reasons_changed
            or external_event_changed
            or ttl < PRESSURE_LATCH_TTL_SECONDS // 2
        ):
            client.set(
                PRESSURE_LATCH_KEY,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
                ex=PRESSURE_LATCH_TTL_SECONDS,
            )
        return True
    except Exception:
        logger.debug("Unable to refresh resource pressure latch", exc_info=True)
        return False


def _clear_pressure_latch(
    redis_client=None,
    *,
    expected_external_event_id: str | None = None,
) -> bool:
    """Atomically record controller recovery before removing its matching latch."""

    try:
        client = _redis_client(redis_client)
        adoptable_event, adoptable_cgroup, adoptable_counter = (
            _adoptable_legacy_cgroup_oom_latch_identity(
                read_resource_pressure_latch(redis_client=client)
            )
            or ("", "", 0)
        )
        cleared = client.eval(
            _CLEAR_PRESSURE_LATCH_LUA,
            5,
            PRESSURE_LATCH_KEY,
            CGROUP_OOM_ACK_HASH_KEY,
            CGROUP_OOM_RECOVERED_HASH_KEY,
            CGROUP_OOM_ACK_SEEN_KEY,
            CGROUP_OOM_EVENT_ID_HASH_KEY,
            str(expected_external_event_id or ""),
            CGROUP_OOM_ACK_RETENTION_SECONDS,
            int(time.time()),
            adoptable_event,
            adoptable_cgroup,
            adoptable_counter,
        )
        return bool(int(cleared or 0))
    except Exception:
        logger.debug("Unable to clear resource pressure latch", exc_info=True)
        return False


def sample_and_publish_resource_pressure(redis_client=None) -> dict[str, Any]:
    # A short-lived shared snapshot is preferred because it contains the most
    # recent metrics.  The independent 24-hour latch survives backend downtime
    # and snapshot expiry, preventing a restart from resetting a paused guard.
    try:
        from app.services.heavy_io import collect_active_resource_leases

        active_leases = collect_active_resource_leases(redis_client=redis_client)
    except Exception as exc:
        active_leases = {
            "mode": "dual_disk_lanes_v2",
            "active": [],
            "active_count": None,
            "reserved_bytes": None,
            "error": type(exc).__name__,
        }

    _monitor.hydrate_baseline_once(redis_client=redis_client)
    _monitor.set_heavy_idle(
        active_leases.get("active_count") == 0 and not active_leases.get("error")
    )
    inherited = read_shared_resource_pressure_snapshot(redis_client=redis_client)
    latched = read_resource_pressure_latch(redis_client=redis_client)
    if not inherited or inherited.get("status") != "paused":
        inherited = latched
    if inherited and inherited.get("status") == "paused":
        _monitor.enforce_external_pause(inherited)
    else:
        _monitor.hydrate_paused_once(inherited)
    previous_status, snapshot = _monitor.sample_with_previous_status()
    reservation = snapshot.setdefault("budget", {}).setdefault("reservation", {})
    reservation.update(
        mode=active_leases.get("mode", "dual_disk_lanes_v2"),
        active_leases=active_leases.get("active") or [],
        active_count=active_leases.get("active_count"),
        reserved_bytes=active_leases.get("reserved_bytes"),
        network_active=active_leases.get("network_active"),
        disk_active=active_leases.get("disk_active"),
        ingest_active=active_leases.get("ingest_active"),
        background_active=active_leases.get("background_active"),
        maintenance_active=active_leases.get("maintenance_active"),
    )
    if active_leases.get("error"):
        reservation["error"] = active_leases["error"]
    _monitor.persist_baseline_if_due(redis_client=redis_client)
    if snapshot.get("status") == "paused":
        _refresh_pressure_latch(snapshot, redis_client=redis_client)
    elif previous_status == "paused":
        # The state machine can only make this transition after all recovery
        # thresholds remained satisfied for the full resume interval.
        latch_controller = (latched or {}).get("controller") or {}
        cleared = _clear_pressure_latch(
            redis_client=redis_client,
            expected_external_event_id=latch_controller.get("external_event_id"),
        )
        if not cleared:
            # A failed compare-clear means the sampled normal state is not
            # authoritative: the latch may have been replaced concurrently or
            # its durable OOM identity may not match the ACK high water.
            retained_latch = (
                read_resource_pressure_latch(redis_client=redis_client)
                or latched
                or {
                    "status": "paused",
                    "reasons": ["pressure_latch_clear_failed"],
                    "trigger_reasons": ["pressure_latch_clear_failed"],
                }
            )
            _monitor.enforce_external_pause(retained_latch)
            snapshot = _monitor.snapshot()
            snapshot["reasons"] = list(
                retained_latch.get("reasons") or ["pressure_latch_clear_failed"]
            )
            snapshot["trigger_reasons"] = list(
                retained_latch.get("trigger_reasons") or snapshot["reasons"]
            )
            retained_controller = retained_latch.get("controller") or {}
            snapshot_controller = snapshot.setdefault("controller", {})
            for key in (
                "external_source",
                "external_event_id",
                "external_cgroup_id",
                "external_oom_kill_counter",
            ):
                if retained_controller.get(key) is not None:
                    snapshot_controller[key] = retained_controller[key]
            snapshot.setdefault("budget", {})["reservation"] = dict(reservation)
            _refresh_pressure_latch(snapshot, redis_client=redis_client)
    publish_resource_pressure_snapshot(snapshot, redis_client=redis_client)
    return snapshot


def get_resource_pressure_snapshot_sync(redis_client=None) -> dict[str, Any]:
    """Return the shared sample, falling back to a local fail-closed monitor."""

    shared = read_shared_resource_pressure_snapshot(redis_client=redis_client)
    # A paused latch is authoritative over an older warning/normal snapshot.
    # This closes the small crash window between persisting the latch and
    # publishing the matching short-lived metrics payload.
    latched = read_resource_pressure_latch(redis_client=redis_client)
    if shared is not None and (latched is None or shared.get("status") == "paused"):
        return shared
    return sample_and_publish_resource_pressure(redis_client=redis_client)


def current_profile_slice_limits_sync(
    workload: str | None,
    *,
    max_work_units: int | None = None,
    max_slice_seconds: float | None = None,
    redis_client=None,
) -> tuple[ResourceSliceLimits, dict[str, Any]]:
    """Read one current snapshot and resolve a slice before workload state opens."""

    snapshot = get_resource_pressure_snapshot_sync(redis_client=redis_client)
    return (
        profile_slice_limits(
            snapshot,
            workload,
            max_work_units=max_work_units,
            max_slice_seconds=max_slice_seconds,
        ),
        snapshot,
    )


async def get_resource_pressure_snapshot() -> dict[str, Any]:
    """Stable async contract used by workload gates across worker processes."""

    return await asyncio.to_thread(get_resource_pressure_snapshot_sync)


async def current_profile_slice_limits(
    workload: str | None,
    *,
    max_work_units: int | None = None,
    max_slice_seconds: float | None = None,
) -> tuple[ResourceSliceLimits, dict[str, Any]]:
    """Async snapshot-and-limit helper for transaction-free admission loops."""

    snapshot = await get_resource_pressure_snapshot()
    return (
        profile_slice_limits(
            snapshot,
            workload,
            max_work_units=max_work_units,
            max_slice_seconds=max_slice_seconds,
        ),
        snapshot,
    )


async def resource_pressure_monitor_loop() -> None:
    interval = max(1.0, settings.resource_pressure_sample_interval_seconds)
    previous_signature: tuple[str, tuple[str, ...]] | None = None
    last_summary_at = 0.0
    while True:
        snapshot = await asyncio.to_thread(sample_and_publish_resource_pressure)
        signature = (
            snapshot["status"],
            snapshot.get("controller_mode"),
            tuple(snapshot.get("reasons") or []),
            snapshot.get("budget", {}).get("throughput_scale"),
        )
        now = time.monotonic()
        if signature != previous_signature:
            logger.log(
                logging.WARNING if snapshot["status"] == "paused" else logging.INFO,
                "Host resource pressure changed status=%s mode=%s reasons=%s "
                "memory_available=%s swap_free_ratio=%s throughput_scale=%s",
                snapshot["status"],
                snapshot.get("controller_mode"),
                snapshot.get("reasons") or [],
                snapshot.get("memory", {}).get("available_bytes"),
                snapshot.get("swap", {}).get("free_ratio"),
                snapshot.get("budget", {}).get("throughput_scale"),
            )
            previous_signature = signature
            last_summary_at = now
        elif now - last_summary_at >= 300:
            logger.debug(
                "Host resource pressure unchanged status=%s memory_available=%s swap_free_ratio=%s",
                snapshot["status"],
                snapshot.get("memory", {}).get("available_bytes"),
                snapshot.get("swap", {}).get("free_ratio"),
            )
            last_summary_at = now
        await asyncio.sleep(interval)


_redis_health_cache: dict[str, Any] | None = None
_redis_health_cache_at = 0.0
_redis_health_cache_lock = threading.Lock()
REDIS_HEALTH_WRITE_PROBE_CACHE_SECONDS = 60.0


def collect_redis_health(redis_client=None, *, write_probe: bool = True) -> dict[str, Any]:
    """Return capacity and writability; PING alone misses ``noeviction`` OOMs."""

    global _redis_health_cache, _redis_health_cache_at
    now = time.monotonic()
    use_cache = write_probe and redis_client is None
    with _redis_health_cache_lock:
        if (
            use_cache
            and _redis_health_cache is not None
            and now - _redis_health_cache_at < REDIS_HEALTH_WRITE_PROBE_CACHE_SECONDS
        ):
            return deepcopy(_redis_health_cache)

    payload = {
        "used_memory_bytes": None,
        "maxmemory_bytes": None,
        "usage_ratio": None,
        "writable": False,
        "rejected_writes": None,
        "oom_rejected_writes": None,
        "application_rejected_enqueues": None,
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "write_probe_cache_seconds": REDIS_HEALTH_WRITE_PROBE_CACHE_SECONDS,
    }
    try:
        client = _redis_client(redis_client)
        memory = client.info("memory")
        used = int(memory.get("used_memory") or 0)
        maximum = int(memory.get("maxmemory") or 0)
        payload.update(
            used_memory_bytes=used,
            maxmemory_bytes=maximum,
            usage_ratio=(used / maximum if maximum > 0 else 0.0),
        )
        oom_rejections: int | None = None
        try:
            errorstats = client.info("errorstats")
            oom = errorstats.get("errorstat_OOM") or {}
            if isinstance(oom, dict):
                oom_rejections = int(oom.get("count") or 0)
        except Exception:
            pass
        application_rejections: int | None = None
        try:
            raw_rejections = client.get(QUEUE_REJECTION_COUNTER_KEY)
            application_rejections = int(raw_rejections or 0)
        except Exception:
            pass
        payload["oom_rejected_writes"] = oom_rejections
        payload["application_rejected_enqueues"] = application_rejections
        known_rejections = [
            value
            for value in (oom_rejections, application_rejections)
            if value is not None
        ]
        payload["rejected_writes"] = sum(known_rejections) if known_rejections else None
        if write_probe:
            probe_key = f"health:redis-write:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
            wrote = client.set(probe_key, b"1", ex=5)
            payload["writable"] = bool(wrote)
            if wrote:
                client.delete(probe_key)
        else:
            payload["writable"] = bool(client.ping())
    except Exception as exc:  # noqa: BLE001 - health payload must stay available
        payload["error"] = type(exc).__name__
    if use_cache:
        with _redis_health_cache_lock:
            _redis_health_cache = deepcopy(payload)
            _redis_health_cache_at = now
    return payload


def collect_queue_worker_health(
    redis_client=None,
    *,
    queue_names: tuple[str, ...] = DEFAULT_QUEUE_NAMES,
) -> tuple[dict[str, int], dict[str, Any]]:
    """Best-effort RQ queue depth, heartbeats, and supervisor circuit state."""

    # ``queues`` is the original compatibility view (plain queued counts).
    # ``queue_activity`` is additive and exposes the registries needed to
    # distinguish work that is merely waiting from work that is already
    # running.  Keeping both avoids breaking existing health consumers.
    queues: dict[str, int] = {}
    workers: dict[str, Any] = {
        "rq": {},
        "supervisors": {},
        "queue_activity": {},
        "cgroup_memory_events": {
            "max": 0,
            "oom": 0,
            "oom_kill": 0,
            "max_delta": 0,
            "oom_delta": 0,
            "oom_kill_delta": 0,
        },
        "cgroup_contribution": {
            "scope": "auto_gallery_worker_cgroups",
            "aggregation": "sum_counters_max_psi",
            "cgroups_reporting": 0,
            "memory": {"current_bytes": 0, "peak_bytes": 0, "limit_bytes": 0},
            "cpu": {
                "usage_usec": 0,
                "usage_usec_delta": 0,
                "usage_cores": 0.0,
                "nr_throttled": 0,
                "nr_throttled_delta": 0,
                "throttled_usec": 0,
                "throttled_usec_delta": 0,
            },
            "io": {
                "read_bytes": 0,
                "read_bytes_delta": 0,
                "read_bytes_per_second": 0.0,
                "write_bytes": 0,
                "write_bytes_delta": 0,
                "write_bytes_per_second": 0.0,
                "read_ios": 0,
                "write_ios": 0,
            },
            "psi": {
                resource: {
                    kind: {window: None for window in ("avg10", "avg60", "avg300")}
                    for kind in ("some", "full")
                }
                for resource in ("memory", "io", "cpu")
            },
            "memory_events": {},
        },
    }
    try:
        from rq import Queue, Worker
        from rq.registry import (
            DeferredJobRegistry,
            ScheduledJobRegistry,
            StartedJobRegistry,
        )

        client = _redis_client(redis_client)
        try:
            from app.services.heavy_io import collect_active_resource_leases

            workers["resource_leases"] = collect_active_resource_leases(client)
        except Exception as exc:
            workers["resource_leases"] = {
                "mode": "dual_disk_lanes_v2",
                "active": [],
                "active_count": None,
                "reserved_bytes": None,
                "error": type(exc).__name__,
            }
        # Queue and registry counts are one Redis pipeline round trip instead
        # of four commands times every queue.  The 15-second health aggregator
        # stays O(queue-count) server work but O(1) network latency.
        queue_keys: list[tuple[str, str, str, str, str]] = []
        count_pipeline = client.pipeline(transaction=False)
        for name in queue_names:
            queue = Queue(name=name, connection=client)
            scheduled_registry = ScheduledJobRegistry(name=name, connection=client)
            deferred_registry = DeferredJobRegistry(name=name, connection=client)
            started_registry = StartedJobRegistry(name=name, connection=client)
            queue_keys.append(
                (
                    name,
                    queue.key,
                    scheduled_registry.key,
                    deferred_registry.key,
                    started_registry.key,
                )
            )
            count_pipeline.llen(queue.key)
            count_pipeline.zcard(scheduled_registry.key)
            count_pipeline.zcard(deferred_registry.key)
            count_pipeline.zcard(started_registry.key)
        try:
            count_values = count_pipeline.execute()
        except Exception:
            count_values = []
        for position, (name, _queue, _scheduled, _deferred, _started) in enumerate(
            queue_keys
        ):
            offset = position * 4
            if len(count_values) < offset + 4:
                queues[name] = -1
                workers["queue_activity"][name] = {
                    "queued": None,
                    "scheduled": None,
                    "deferred": None,
                    "waiting": None,
                    "running": None,
                }
                continue
            queued, scheduled, deferred, running = (
                int(value or 0) for value in count_values[offset:offset + 4]
            )
            queues[name] = queued
            workers["queue_activity"][name] = {
                "queued": queued,
                "scheduled": scheduled,
                "deferred": deferred,
                "waiting": queued + scheduled + deferred,
                "running": running,
            }
        cgroup_reports: dict[str, dict[str, Any]] = {}
        try:
            for worker in Worker.all(connection=client):
                last_heartbeat = getattr(worker, "last_heartbeat", None)
                worker_payload = {
                    "state": worker.get_state(),
                    "queues": list(worker.queue_names()),
                    "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
                }
                try:
                    base_fields = (
                        "resource_pressure_status",
                        "resource_pressure_reasons",
                        "resource_local_throughput_scale",
                        "resource_local_hard_gate",
                        "cgroup_id",
                        "cgroup_sampled_at",
                        "cgroup_metrics_available",
                        "cgroup_memory_max_events",
                        "cgroup_memory_oom_events",
                        "cgroup_memory_oom_kill_events",
                        "cgroup_memory_max_delta",
                        "cgroup_memory_oom_delta",
                        "cgroup_memory_oom_kill_delta",
                        "cgroup_memory_current_bytes",
                        "cgroup_memory_peak_bytes",
                        "cgroup_memory_limit_bytes",
                        "cgroup_cpu_usage_usec",
                        "cgroup_cpu_usage_usec_delta",
                        "cgroup_cpu_usage_cores",
                        "cgroup_cpu_nr_throttled",
                        "cgroup_cpu_nr_throttled_delta",
                        "cgroup_cpu_throttled_usec",
                        "cgroup_cpu_throttled_usec_delta",
                        "cgroup_io_read_bytes",
                        "cgroup_io_read_bytes_delta",
                        "cgroup_io_read_bytes_per_second",
                        "cgroup_io_write_bytes",
                        "cgroup_io_write_bytes_delta",
                        "cgroup_io_write_bytes_per_second",
                        "cgroup_io_read_ios",
                        "cgroup_io_write_ios",
                    )
                    psi_fields = tuple(
                        f"cgroup_psi_{resource}_{kind}_{window}"
                        for resource in ("memory", "io", "cpu")
                        for kind in ("some", "full")
                        for window in ("avg10", "avg60", "avg300")
                    )
                    field_names = (*base_fields, *psi_fields)
                    raw_values = client.hmget(worker.key, *field_names)
                    values = {
                        name: (
                            value.decode("utf-8", "replace")
                            if isinstance(value, bytes)
                            else value
                        )
                        for name, value in zip(field_names, raw_values)
                    }

                    def as_int(name: str) -> int:
                        try:
                            return int(float(values.get(name) or 0))
                        except (TypeError, ValueError):
                            return 0

                    def as_float(name: str) -> float:
                        try:
                            return float(values.get(name) or 0.0)
                        except (TypeError, ValueError):
                            return 0.0

                    pressure_status = values.get("resource_pressure_status")
                    pressure_reasons = values.get("resource_pressure_reasons")
                    worker_payload["resource_pressure_status"] = pressure_status
                    worker_payload["resource_pressure_reasons"] = (
                        str(pressure_reasons).split(",") if pressure_reasons else []
                    )
                    worker_payload["resource_local_throughput_scale"] = (
                        as_float("resource_local_throughput_scale")
                        if values.get("resource_local_throughput_scale") is not None
                        else None
                    )
                    worker_payload["resource_local_hard_gate"] = (
                        str(values.get("resource_local_hard_gate")) == "1"
                    )
                    worker_payload["cgroup_memory_events"] = {
                        "max": as_int("cgroup_memory_max_events"),
                        "oom": as_int("cgroup_memory_oom_events"),
                        "oom_kill": as_int("cgroup_memory_oom_kill_events"),
                        "max_delta": as_int("cgroup_memory_max_delta"),
                        "oom_delta": as_int("cgroup_memory_oom_delta"),
                        "oom_kill_delta": as_int("cgroup_memory_oom_kill_delta"),
                        "scope": "worker_cgroup",
                    }
                    contribution = {
                        "cgroup_id": str(values.get("cgroup_id") or worker.name),
                        "sampled_at": values.get("cgroup_sampled_at"),
                        "available": str(values.get("cgroup_metrics_available")) == "1",
                        "memory": {
                            "current_bytes": as_int("cgroup_memory_current_bytes"),
                            "peak_bytes": as_int("cgroup_memory_peak_bytes"),
                            "limit_bytes": as_int("cgroup_memory_limit_bytes"),
                        },
                        "cpu": {
                            "usage_usec": as_int("cgroup_cpu_usage_usec"),
                            "usage_usec_delta": as_int("cgroup_cpu_usage_usec_delta"),
                            "usage_cores": as_float("cgroup_cpu_usage_cores"),
                            "nr_throttled": as_int("cgroup_cpu_nr_throttled"),
                            "nr_throttled_delta": as_int("cgroup_cpu_nr_throttled_delta"),
                            "throttled_usec": as_int("cgroup_cpu_throttled_usec"),
                            "throttled_usec_delta": as_int("cgroup_cpu_throttled_usec_delta"),
                        },
                        "io": {
                            "read_bytes": as_int("cgroup_io_read_bytes"),
                            "read_bytes_delta": as_int("cgroup_io_read_bytes_delta"),
                            "read_bytes_per_second": as_float("cgroup_io_read_bytes_per_second"),
                            "write_bytes": as_int("cgroup_io_write_bytes"),
                            "write_bytes_delta": as_int("cgroup_io_write_bytes_delta"),
                            "write_bytes_per_second": as_float("cgroup_io_write_bytes_per_second"),
                            "read_ios": as_int("cgroup_io_read_ios"),
                            "write_ios": as_int("cgroup_io_write_ios"),
                        },
                        "psi": {
                            resource: {
                                kind: {
                                    window: as_float(
                                        f"cgroup_psi_{resource}_{kind}_{window}"
                                    )
                                    for window in ("avg10", "avg60", "avg300")
                                }
                                for kind in ("some", "full")
                            }
                            for resource in ("memory", "io", "cpu")
                        },
                        "memory_events": worker_payload["cgroup_memory_events"],
                    }
                    worker_payload["cgroup_contribution"] = contribution
                    previous = cgroup_reports.get(contribution["cgroup_id"])
                    if previous is None or str(contribution.get("sampled_at") or "") > str(
                        previous.get("sampled_at") or ""
                    ):
                        cgroup_reports[contribution["cgroup_id"]] = contribution
                except Exception:
                    pass
                workers["rq"][worker.name] = worker_payload
        except Exception as exc:
            workers["rq_error"] = type(exc).__name__

        aggregate = workers["cgroup_contribution"]
        for contribution in cgroup_reports.values():
            for event_name in (
                "max",
                "oom",
                "oom_kill",
                "max_delta",
                "oom_delta",
                "oom_kill_delta",
            ):
                workers["cgroup_memory_events"][event_name] += contribution[
                    "memory_events"
                ][event_name]
            if not contribution.get("available"):
                continue
            aggregate["cgroups_reporting"] += 1
            for name in ("current_bytes", "peak_bytes", "limit_bytes"):
                aggregate["memory"][name] += contribution["memory"][name]
            for name in aggregate["cpu"]:
                aggregate["cpu"][name] += contribution["cpu"][name]
            for name in aggregate["io"]:
                aggregate["io"][name] += contribution["io"][name]
            for resource in ("memory", "io", "cpu"):
                for kind in ("some", "full"):
                    for window in ("avg10", "avg60", "avg300"):
                        value = contribution["psi"][resource][kind][window]
                        current = aggregate["psi"][resource][kind][window]
                        aggregate["psi"][resource][kind][window] = (
                            value if current is None else max(current, value)
                        )
        aggregate["memory_events"] = dict(workers["cgroup_memory_events"])

        # A worker OOM kill is the only cgroup event that crosses into the
        # shared hard latch.  memory.max and allocation-only oom events stay
        # worker-local soft feedback so one constrained container cannot pause
        # every queue.
        promote_worker_cgroup_oom_kills(
            cgroup_reports.values(),
            redis_client=client,
        )

        try:
            raw_supervisors = client.hgetall(WORKER_SUPERVISOR_HASH_KEY) or {}
            for raw_name, raw in raw_supervisors.items():
                name = raw_name.decode() if isinstance(raw_name, bytes) else str(raw_name)
                raw_text = raw.decode() if isinstance(raw, bytes) else str(raw)
                payload = json.loads(raw_text)
                try:
                    valid_until = datetime.fromisoformat(str(payload.get("valid_until")))
                    if valid_until.tzinfo is None:
                        valid_until = valid_until.replace(tzinfo=timezone.utc)
                    payload["stale"] = valid_until <= datetime.now(timezone.utc)
                except (AttributeError, TypeError, ValueError):
                    payload["stale"] = True
                if payload["stale"]:
                    try:
                        client.eval(
                            "if redis.call('hget', KEYS[1], ARGV[1]) == "
                            "ARGV[2] then return redis.call('hdel', KEYS[1], "
                            "ARGV[1]) end return 0",
                            1,
                            WORKER_SUPERVISOR_HASH_KEY,
                            raw_name,
                            raw,
                        )
                    except Exception:
                        logger.debug(
                            "Unable to prune stale worker supervisor %s",
                            name,
                            exc_info=True,
                        )
                    continue
                workers["supervisors"][name] = payload

            # During a rolling upgrade the former TTL keys may coexist for at
            # most 75 seconds.  Avoid a full-keyspace SCAN once all four known
            # service classes report through the fixed hash.
            reported_classes = {
                "downloads"
                if any(str(value).startswith("downloads") for value in payload.get("queues", []))
                else "imports"
                if "imports" in payload.get("queues", [])
                else "operations"
                if "operations" in payload.get("queues", [])
                else "scheduled"
                if "scheduled" in payload.get("queues", [])
                else "unknown"
                for payload in workers["supervisors"].values()
                if isinstance(payload, dict) and not payload.get("stale")
            }
            if not {"downloads", "imports", "operations", "scheduled"}.issubset(
                reported_classes
            ):
                for key in client.scan_iter(match=f"{WORKER_SUPERVISOR_PREFIX}*"):
                    raw = client.get(key)
                    if not raw:
                        continue
                    key_text = key.decode() if isinstance(key, bytes) else str(key)
                    raw_text = raw.decode() if isinstance(raw, bytes) else str(raw)
                    value = json.loads(raw_text)
                    workers["supervisors"].setdefault(
                        key_text.removeprefix(WORKER_SUPERVISOR_PREFIX),
                        value,
                    )
        except Exception as exc:
            workers["supervisor_error"] = type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - isolate Redis/RQ health failures
        workers["error"] = type(exc).__name__
    return queues, workers
