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

logger = logging.getLogger(__name__)

PRESSURE_SNAPSHOT_KEY = "resource:pressure:snapshot"
PRESSURE_LATCH_KEY = "resource:pressure:latch"
RESOURCE_CONTROL_CHANNEL = "resource:control"
PRESSURE_LATCH_TTL_SECONDS = 24 * 60 * 60
PRESSURE_BASELINE_KEY = "resource:pressure:baseline:v1"
PRESSURE_BASELINE_WINDOW_SECONDS = 24 * 60 * 60
PRESSURE_BASELINE_SAMPLE_SECONDS = 60.0
PRESSURE_BASELINE_PERSIST_SECONDS = 5 * 60.0
PRESSURE_BASELINE_MIN_SAMPLES = 10
WORKER_SUPERVISOR_PREFIX = "worker:supervisor:"
WORKER_SUPERVISOR_HASH_KEY = "worker:supervisors:v1"
GIB = 1024 ** 3
MIB = 1024 ** 2

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


@dataclass(frozen=True)
class PressureThresholds:
    warning_available_bytes: int = int(1.5 * GIB)
    pause_available_bytes: int = int(1.25 * GIB)
    resume_available_bytes: int = int(1.75 * GIB)
    warning_swap_free_ratio: float = 0.30
    pause_swap_free_ratio: float = 0.25
    critical_swap_free_ratio: float = 0.15
    swap_activity_bytes_per_second: float = 64.0 * MIB / 60.0
    resume_swap_free_ratio: float = 0.30
    pause_memory_psi: float = 2.0
    pause_io_psi: float = 15.0
    resume_memory_psi: float = 1.0
    resume_io_psi: float = 5.0
    pause_samples: int = 3
    failure_samples: int = 3
    resume_seconds: float = 60.0
    memory_reserve_mode: str = "fixed"
    memory_reserve_ratio: float | None = None
    memory_reserve_min_bytes: int | None = None
    memory_reserve_max_bytes: int | None = None
    detected_memory_total_bytes: int | None = None


@dataclass(frozen=True)
class ResourceSample:
    memory_total_bytes: int
    memory_available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    memory_full_avg10: float | None = None
    memory_full_avg60: float | None = None
    memory_full_avg300: float | None = None
    io_full_avg10: float | None = None
    io_full_avg60: float | None = None
    io_full_avg300: float | None = None
    swap_in_bytes_per_second: float | None = None
    swap_out_bytes_per_second: float | None = None
    memory_available_change_bytes_per_second: float | None = None
    cgroup_memory_max_events: int | None = None
    cgroup_memory_oom_events: int | None = None
    cgroup_memory_oom_kill_events: int | None = None
    cgroup_memory_max_delta: int | None = None
    cgroup_memory_oom_delta: int | None = None
    cgroup_memory_oom_kill_delta: int | None = None
    foreground_p95_ms: float | None = None
    foreground_sample_count: int = 0
    baseline_memory_psi_median: float | None = None
    baseline_memory_psi_p95: float | None = None
    baseline_io_psi_median: float | None = None
    baseline_io_psi_p95: float | None = None
    baseline_sample_count: int = 0
    baseline_idle_observation: bool = False
    memory_psi_soft_trigger: float | None = None
    io_psi_soft_trigger: float | None = None
    sampled_at: str | None = None

    @property
    def memory_available_ratio(self) -> float | None:
        if self.memory_total_bytes <= 0:
            return None
        return self.memory_available_bytes / self.memory_total_bytes

    @property
    def swap_free_ratio(self) -> float:
        # A host with no configured swap cannot be "out of swap".
        if self.swap_total_bytes <= 0:
            return 1.0
        return self.swap_free_bytes / self.swap_total_bytes

    @property
    def swap_activity_bytes_per_second(self) -> float | None:
        rates = (
            value
            for value in (self.swap_in_bytes_per_second, self.swap_out_bytes_per_second)
            if value is not None
        )
        values = list(rates)
        return sum(values) if values else None


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


@dataclass(frozen=True)
class ResourceProfile:
    """A measured working-set reservation and a cooperative work-slice hint."""

    name: str
    memory_reservation_bytes: int
    base_slice_seconds: float | None
    base_work_units: int


@dataclass(frozen=True)
class ResourceSliceLimits:
    """One bounded cooperative work slice derived from a controller snapshot.

    ``allowed`` is the hard safety decision.  ``work_units`` and
    ``slice_seconds`` are soft AIMD limits and must be applied before opening a
    database transaction or taking a profile lease.  A denied slice always has
    zero units, so a caller cannot accidentally turn a critical snapshot into
    the controller's minimum-progress trickle.
    """

    profile: str
    allowed: bool
    work_units: int
    slice_seconds: float | None
    governance_mode: str
    controller_mode: str
    effective_scale: float
    reason: str | None = None


RESOURCE_PROFILES: dict[str, ResourceProfile] = {
    "light": ResourceProfile("light", 0, 30.0, 100),
    "download_network": ResourceProfile("download_network", 128 * MIB, None, 1),
    "import_db": ResourceProfile("import_db", 96 * MIB, 20.0, 25),
    "image_derive": ResourceProfile("image_derive", 256 * MIB, 20.0, 1),
    "video_derive": ResourceProfile("video_derive", 384 * MIB, 20.0, 1),
    "search_index": ResourceProfile("search_index", 192 * MIB, 20.0, 2_000),
    # A Git projection outbox unit is one commit, and one import commit may
    # contain up to 25 work changes.  Releasing after one commit bounds the real
    # filesystem/DB work rather than the coordinator row count.
    "git_projection": ResourceProfile("git_projection", 128 * MIB, 20.0, 1),
    "maintenance": ResourceProfile("maintenance", 256 * MIB, 20.0, 1),
}

RESOURCE_PROFILE_ALIASES = {
    "download": "download_network",
    "downloads": "download_network",
    "import": "import_db",
    "imports": "import_db",
    "import-projection": "import_db",
    "media": "video_derive",
    "image": "image_derive",
    "video": "video_derive",
    "search": "search_index",
    "meili": "search_index",
    "git": "git_projection",
    "gitllery": "git_projection",
    "operations": "maintenance",
    "backup": "maintenance",
    "sqlite": "maintenance",
    "vacuum": "maintenance",
    "dedup": "image_derive",
}


def _configured_enforced_profiles() -> set[str] | None:
    """Return the staged soft-enforcement allowlist.

    ``None`` preserves the pre-rollout meaning of enforce-without-a-list: all
    profiles. An explicit comma-separated list narrows only soft AIMD; hard
    critical admission is never scoped.
    """

    raw = str(settings.resource_governance_enforced_profiles or "").strip()
    if not raw:
        return None
    return {
        workload_profile_name(value)
        for value in raw.split(",")
        if value.strip()
    }


def profile_soft_budget_enforced(profile_name: str) -> bool:
    if str(settings.resource_governance_mode).strip().lower() != "enforce":
        return False
    configured = _configured_enforced_profiles()
    return configured is None or profile_name in configured


def workload_profile_name(workload: str | None) -> str:
    normalized = str(workload or "light").strip().lower().replace("_", "-")
    direct = RESOURCE_PROFILE_ALIASES.get(normalized)
    if direct:
        return direct
    if normalized.startswith("download"):
        return "download_network"
    if normalized.startswith("import"):
        return "import_db"
    if "media-derivative" in normalized or "media-derive" in normalized:
        return "video_derive"
    if "image" in normalized or "asset-dedup" in normalized:
        return "image_derive"
    if "video" in normalized or "ffmpeg" in normalized:
        return "video_derive"
    if normalized.startswith("search") or "meili" in normalized:
        return "search_index"
    if "gitllery-projection" in normalized or normalized.startswith("git-projection"):
        return "git_projection"
    if "operation:gitllery" in normalized:
        return "maintenance"
    if "gitllery" in normalized or normalized.startswith("git"):
        return "git_projection"
    if (
        normalized.startswith("backup")
        or normalized.startswith("sqlite")
        or "vacuum" in normalized
        or normalized.startswith("operation")
    ):
        return "maintenance"
    if normalized in {"light", "scheduled", "default"}:
        return "light"
    return "maintenance"


def automatic_memory_reserve_bytes(
    memory_total_bytes: int,
    *,
    ratio: float = 0.15,
    minimum_bytes: int = 384 * MIB,
    maximum_bytes: int = 1280 * MIB,
) -> int:
    """Return a bounded device-relative reserve for project admission.

    This is deliberately based on total memory rather than a transient idle
    sample. It therefore adapts to small and large devices without learning a
    pathological background workload as normal.
    """

    lower = max(128 * MIB, int(minimum_bytes))
    upper = max(lower, int(maximum_bytes))
    proportional = int(max(0, int(memory_total_bytes)) * max(0.01, float(ratio)))
    return min(upper, max(lower, proportional))


def _detected_memory_total_bytes() -> int | None:
    try:
        return _parse_meminfo(Path("/proc/meminfo"))["MemTotal"]
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def thresholds_from_settings(
    memory_total_bytes: int | None = None,
) -> PressureThresholds:
    reserve_mode = str(settings.resource_memory_reserve_mode).strip().lower()
    detected_total = (
        _detected_memory_total_bytes()
        if memory_total_bytes is None
        else max(0, int(memory_total_bytes))
    )
    reserve_min = settings.resource_memory_reserve_min_mb * MIB
    reserve_max = settings.resource_memory_reserve_max_mb * MIB
    if reserve_mode == "auto" and detected_total:
        pause_available = automatic_memory_reserve_bytes(
            detected_total,
            ratio=settings.resource_memory_reserve_ratio,
            minimum_bytes=reserve_min,
            maximum_bytes=reserve_max,
        )
    else:
        reserve_mode = "fixed"
        pause_available = settings.resource_pressure_pause_available_mb * MIB
    warning_available = max(
        pause_available + 128 * MIB,
        int(pause_available * 1.20),
    )
    resume_available = max(
        pause_available + 256 * MIB,
        int(pause_available * 1.40),
    )
    return PressureThresholds(
        warning_available_bytes=(
            warning_available
            if reserve_mode == "auto"
            else settings.resource_pressure_warning_available_mb * MIB
        ),
        pause_available_bytes=pause_available,
        resume_available_bytes=(
            resume_available
            if reserve_mode == "auto"
            else settings.resource_pressure_resume_available_mb * MIB
        ),
        warning_swap_free_ratio=settings.resource_pressure_warning_swap_free_ratio,
        pause_swap_free_ratio=settings.resource_pressure_pause_swap_free_ratio,
        critical_swap_free_ratio=settings.resource_pressure_critical_swap_free_ratio,
        swap_activity_bytes_per_second=(
            settings.resource_pressure_swap_activity_mb_per_minute * MIB / 60.0
        ),
        resume_swap_free_ratio=settings.resource_pressure_resume_swap_free_ratio,
        pause_memory_psi=settings.resource_pressure_pause_memory_psi_full_avg10,
        pause_io_psi=settings.resource_pressure_pause_io_psi_full_avg10,
        resume_memory_psi=settings.resource_pressure_resume_memory_psi_full_avg10,
        resume_io_psi=settings.resource_pressure_resume_io_psi_full_avg10,
        pause_samples=max(1, settings.resource_pressure_pause_samples),
        failure_samples=max(1, settings.resource_pressure_failure_samples),
        resume_seconds=max(0.0, settings.resource_pressure_resume_seconds),
        memory_reserve_mode=reserve_mode,
        memory_reserve_ratio=(
            float(settings.resource_memory_reserve_ratio)
            if reserve_mode == "auto"
            else None
        ),
        memory_reserve_min_bytes=reserve_min if reserve_mode == "auto" else None,
        memory_reserve_max_bytes=reserve_max if reserve_mode == "auto" else None,
        detected_memory_total_bytes=detected_total,
    )


def _parse_meminfo(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            key, separator, raw = line.partition(":")
            if not separator:
                continue
            parts = raw.strip().split()
            if not parts:
                continue
            value = int(parts[0])
            if len(parts) > 1 and parts[1].lower() == "kb":
                value *= 1024
            values[key] = value
    required = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    missing = required.difference(values)
    if missing:
        raise ValueError(f"meminfo missing fields: {', '.join(sorted(missing))}")
    return values


def current_memory_available_bytes(
    proc_root: str | os.PathLike[str] = "/proc",
) -> int:
    """Cheap grant-time RAM check used in addition to the shared snapshot."""

    return _parse_meminfo(Path(proc_root) / "meminfo")["MemAvailable"]


def _parse_psi_full(path: Path) -> dict[str, float] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if not parts or parts[0] != "full":
                    continue
                values: dict[str, float] = {}
                for field in parts[1:]:
                    key, separator, raw = field.partition("=")
                    if separator and key in {"avg10", "avg60", "avg300"}:
                        values[key] = float(raw)
                return values or None
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        # PSI is optional on older kernels.  Core memory/swap sampling remains
        # authoritative and must continue when these files do not exist.
        return None
    return None


def _parse_psi_full_avg10(path: Path) -> float | None:
    """Compatibility helper retained for focused parser callers/tests."""

    values = _parse_psi_full(path)
    return values.get("avg10") if values else None


def sample_cgroup_memory_events(
    cgroup_root: str | os.PathLike[str] = "/sys/fs/cgroup",
) -> dict[str, int | None]:
    values: dict[str, int] = {}
    try:
        with (Path(cgroup_root) / "memory.events").open(encoding="utf-8") as handle:
            for line in handle:
                key, separator, raw = line.partition(" ")
                if separator and key in {"max", "oom", "oom_kill"}:
                    values[key] = int(raw.strip())
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return {
        "max": values.get("max"),
        "oom": values.get("oom"),
        "oom_kill": values.get("oom_kill"),
    }


def _read_optional_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def _read_cgroup_memory_limit(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return None if raw == "max" else int(raw)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def _read_cgroup_stat(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                key, separator, raw = line.partition(" ")
                if separator:
                    values[key] = int(raw.strip())
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return {}
    return values


def _read_cgroup_io_stat(path: Path) -> dict[str, int]:
    totals = {
        "read_bytes": 0,
        "write_bytes": 0,
        "read_ios": 0,
        "write_ios": 0,
        "discard_bytes": 0,
        "discard_ios": 0,
    }
    field_names = {
        "rbytes": "read_bytes",
        "wbytes": "write_bytes",
        "rios": "read_ios",
        "wios": "write_ios",
        "dbytes": "discard_bytes",
        "dios": "discard_ios",
    }
    seen = False
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                for field in line.split()[1:]:
                    key, separator, raw = field.partition("=")
                    output_name = field_names.get(key)
                    if separator and output_name:
                        totals[output_name] += int(raw)
                        seen = True
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return {}
    return totals if seen else {}


def _read_cgroup_pressure(path: Path) -> dict[str, dict[str, float]]:
    pressure: dict[str, dict[str, float]] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if not parts or parts[0] not in {"some", "full"}:
                    continue
                values: dict[str, float] = {}
                for field in parts[1:]:
                    key, separator, raw = field.partition("=")
                    if separator and key in {"avg10", "avg60", "avg300"}:
                        values[key] = float(raw)
                if values:
                    pressure[parts[0]] = values
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return {}
    return pressure


def sample_cgroup_contribution(
    cgroup_root: str | os.PathLike[str] = "/sys/fs/cgroup",
) -> dict[str, Any]:
    """Read cheap cgroup-v2 contribution counters without Docker privileges."""

    root = Path(cgroup_root)
    cgroup_id = str(root)
    if root == Path("/sys/fs/cgroup"):
        try:
            membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
            cgroup_id = next(
                (
                    line.split("::", 1)[1].strip()
                    for line in membership.splitlines()
                    if "::" in line
                ),
                cgroup_id,
            )
            cgroup_id = f"{socket.gethostname()}:{cgroup_id}"
        except (FileNotFoundError, PermissionError, OSError, IndexError):
            pass
    cpu = _read_cgroup_stat(root / "cpu.stat")
    io = _read_cgroup_io_stat(root / "io.stat")
    return {
        "memory": {
            "current_bytes": _read_optional_int(root / "memory.current"),
            "peak_bytes": _read_optional_int(root / "memory.peak"),
            "limit_bytes": _read_cgroup_memory_limit(root / "memory.max"),
        },
        "cpu": {
            "usage_usec": cpu.get("usage_usec"),
            "user_usec": cpu.get("user_usec"),
            "system_usec": cpu.get("system_usec"),
            "nr_periods": cpu.get("nr_periods"),
            "nr_throttled": cpu.get("nr_throttled"),
            "throttled_usec": cpu.get("throttled_usec"),
        },
        "io": {
            "read_bytes": io.get("read_bytes"),
            "write_bytes": io.get("write_bytes"),
            "read_ios": io.get("read_ios"),
            "write_ios": io.get("write_ios"),
            "discard_bytes": io.get("discard_bytes"),
            "discard_ios": io.get("discard_ios"),
        },
        "psi": {
            "memory": _read_cgroup_pressure(root / "memory.pressure"),
            "io": _read_cgroup_pressure(root / "io.pressure"),
            "cpu": _read_cgroup_pressure(root / "cpu.pressure"),
        },
        "memory_events": sample_cgroup_memory_events(root),
        "cgroup_id": cgroup_id,
        "scope": "current_cgroup",
    }


def _parse_vmstat_swap_pages(path: Path) -> tuple[int, int] | None:
    try:
        values: dict[str, int] = {}
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                key, separator, raw = line.partition(" ")
                if key in {"pswpin", "pswpout"} and separator:
                    values[key] = int(raw.strip())
        if {"pswpin", "pswpout"}.issubset(values):
            return values["pswpin"], values["pswpout"]
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None
    return None


_swap_rate_lock = threading.Lock()
_swap_rate_previous: dict[str, tuple[float, int, int, int]] = {}
_cgroup_event_previous: dict[str, tuple[int | None, int | None, int | None]] = {}
_foreground_latency_lock = threading.Lock()
_foreground_latencies: deque[tuple[float, float]] = deque(maxlen=4096)
FOREGROUND_LATENCY_WINDOW_SECONDS = 5 * 60


def record_foreground_latency(path: str, duration_ms: float) -> None:
    """Middleware hook for work-list/search GET latency feedback."""

    normalized = str(path or "")
    if not (
        normalized.startswith("/api/v1/search")
        or normalized.startswith("/api/v1/works")
    ):
        return
    try:
        value = max(0.0, float(duration_ms))
    except (TypeError, ValueError):
        return
    now = time.monotonic()
    with _foreground_latency_lock:
        _foreground_latencies.append((now, value))


def foreground_latency_snapshot(now: float | None = None) -> dict[str, Any]:
    now = time.monotonic() if now is None else now
    cutoff = now - FOREGROUND_LATENCY_WINDOW_SECONDS
    with _foreground_latency_lock:
        while _foreground_latencies and _foreground_latencies[0][0] < cutoff:
            _foreground_latencies.popleft()
        values = sorted(value for _, value in _foreground_latencies)
    if not values:
        return {"p95_ms": None, "sample_count": 0, "window_seconds": 300}
    index = min(len(values) - 1, max(0, int(0.95 * (len(values) - 1))))
    return {
        "p95_ms": round(values[index], 3),
        "sample_count": len(values),
        "window_seconds": FOREGROUND_LATENCY_WINDOW_SECONDS,
    }


def _sample_trend_rates(
    root: Path,
    memory_available_bytes: int,
) -> tuple[float | None, float | None, float | None]:
    counters = _parse_vmstat_swap_pages(root / "vmstat")
    if counters is None:
        return None, None, None
    now = time.monotonic()
    cache_key = str(root.resolve())
    with _swap_rate_lock:
        previous = _swap_rate_previous.get(cache_key)
        _swap_rate_previous[cache_key] = (
            now,
            counters[0],
            counters[1],
            memory_available_bytes,
        )
    if previous is None or now <= previous[0]:
        return 0.0, 0.0, 0.0
    elapsed = now - previous[0]
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    swap_in = max(0, counters[0] - previous[1]) * page_size / elapsed
    swap_out = max(0, counters[1] - previous[2]) * page_size / elapsed
    memory_change = (memory_available_bytes - previous[3]) / elapsed
    return swap_in, swap_out, memory_change


def _sample_cgroup_event_deltas(
    cgroup_root: Path,
) -> tuple[dict[str, int | None], dict[str, int | None]]:
    current = sample_cgroup_memory_events(cgroup_root)
    cache_key = str(cgroup_root.resolve())
    current_tuple = (current["max"], current["oom"], current["oom_kill"])
    with _swap_rate_lock:
        previous = _cgroup_event_previous.get(cache_key)
        _cgroup_event_previous[cache_key] = current_tuple
    deltas: dict[str, int | None] = {}
    for index, key in enumerate(("max", "oom", "oom_kill")):
        value = current_tuple[index]
        old = previous[index] if previous is not None else value
        deltas[key] = (
            max(0, int(value) - int(old))
            if value is not None and old is not None
            else None
        )
    return current, deltas


def sample_resource_metrics(proc_root: str | os.PathLike[str] = "/proc") -> ResourceSample:
    root = Path(proc_root)
    meminfo = _parse_meminfo(root / "meminfo")
    swap_in_rate, swap_out_rate, memory_change_rate = _sample_trend_rates(
        root,
        meminfo["MemAvailable"],
    )
    memory_psi = _parse_psi_full(root / "pressure" / "memory") or {}
    io_psi = _parse_psi_full(root / "pressure" / "io") or {}
    if root == Path("/proc"):
        cgroup_events, cgroup_deltas = _sample_cgroup_event_deltas(Path("/sys/fs/cgroup"))
    else:
        cgroup_events = {"max": None, "oom": None, "oom_kill": None}
        cgroup_deltas = {"max": None, "oom": None, "oom_kill": None}
    foreground = foreground_latency_snapshot()
    return ResourceSample(
        memory_total_bytes=meminfo["MemTotal"],
        memory_available_bytes=meminfo["MemAvailable"],
        swap_total_bytes=meminfo["SwapTotal"],
        swap_free_bytes=meminfo["SwapFree"],
        memory_full_avg10=memory_psi.get("avg10"),
        memory_full_avg60=memory_psi.get("avg60"),
        memory_full_avg300=memory_psi.get("avg300"),
        io_full_avg10=io_psi.get("avg10"),
        io_full_avg60=io_psi.get("avg60"),
        io_full_avg300=io_psi.get("avg300"),
        swap_in_bytes_per_second=swap_in_rate,
        swap_out_bytes_per_second=swap_out_rate,
        memory_available_change_bytes_per_second=memory_change_rate,
        cgroup_memory_max_events=cgroup_events["max"],
        cgroup_memory_oom_events=cgroup_events["oom"],
        cgroup_memory_oom_kill_events=cgroup_events["oom_kill"],
        cgroup_memory_max_delta=cgroup_deltas["max"],
        cgroup_memory_oom_delta=cgroup_deltas["oom"],
        cgroup_memory_oom_kill_delta=cgroup_deltas["oom_kill"],
        foreground_p95_ms=foreground["p95_ms"],
        foreground_sample_count=foreground["sample_count"],
        sampled_at=datetime.now(timezone.utc).isoformat(),
    )


class ResourcePressureStateMachine:
    """Hard safety gate plus a soft AIMD throughput controller.

    ``controller_mode`` is the new internal contract.  The legacy ``status``
    remains additive and maps normal/constrained/critical to
    normal/warning/paused, so old workers and the admin UI remain safe during a
    rolling deployment.
    """

    def __init__(
        self,
        thresholds: PressureThresholds | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.thresholds = thresholds or thresholds_from_settings()
        self.clock = clock
        self.controller_mode = "normal"
        self.status = "normal"
        self.reasons: list[str] = []
        self.throughput_scale = 1.0
        self._generation = 0
        self._pause_count = 0
        self._failure_count = 0
        self._recovery_started_at: float | None = None
        self._stable_since: float | None = None
        self._last_increase_at: float | None = None
        self._foreground_slow_count = 0
        self._sample: ResourceSample | None = None
        self._sampled_at: str | None = None

    @staticmethod
    def _legacy_status(mode: str) -> str:
        return {"normal": "normal", "constrained": "warning", "critical": "paused"}[mode]

    def _set_mode(self, mode: str) -> None:
        if mode != self.controller_mode:
            self._generation += 1
        self.controller_mode = mode
        self.status = self._legacy_status(mode)

    def _set_scale(self, value: float) -> None:
        value = round(max(0.0, min(1.0, value)), 4)
        if value != self.throughput_scale:
            self._generation += 1
        self.throughput_scale = value

    def _decrease_budget(self) -> None:
        minimum = max(0.01, min(1.0, float(settings.resource_budget_min_scale)))
        factor = max(0.05, min(0.95, float(settings.resource_budget_decrease_factor)))
        self._set_scale(max(minimum, self.throughput_scale * factor))

    def _increase_budget(self, now: float) -> None:
        step = max(0.001, min(0.25, float(settings.resource_budget_increase_step)))
        floor = max(0.01, min(1.0, float(settings.resource_budget_min_scale)))
        self._set_scale(min(1.0, max(floor, self.throughput_scale) + step))
        self._last_increase_at = now

    def _reset_stable_window(self) -> None:
        self._stable_since = None

    def _recover_budget_if_stable(self, now: float) -> None:
        if self.throughput_scale >= 1.0:
            self._stable_since = self._stable_since or now
            return
        if self._stable_since is None:
            self._stable_since = now
            return
        stable_seconds = max(
            1.0,
            float(settings.resource_budget_increase_stable_seconds),
        )
        if now - self._stable_since < stable_seconds:
            return
        if (
            self._last_increase_at is not None
            and now - self._last_increase_at < stable_seconds
        ):
            return
        self._increase_budget(now)

    def restore_paused(self, reasons: list[str] | None = None) -> dict[str, Any]:
        """Restore a cross-process critical latch without skipping hysteresis."""

        self._set_mode("critical")
        self._set_scale(0.0)
        self.reasons = list(reasons or ["restored_pressure_latch"])
        self._pause_count = self.thresholds.pause_samples
        self._failure_count = 0
        self._recovery_started_at = None
        self._reset_stable_window()
        return self.snapshot()

    def _hard_reasons(self, sample: ResourceSample) -> list[str]:
        t = self.thresholds
        reasons = []
        if sample.memory_available_bytes < t.pause_available_bytes:
            reasons.append("memory_available_critical")
        if sample.swap_free_ratio < t.critical_swap_free_ratio:
            reasons.append("swap_free_critical")
        swap_activity = sample.swap_activity_bytes_per_second
        if (
            sample.swap_free_ratio < t.pause_swap_free_ratio
            and swap_activity is not None
            and swap_activity >= t.swap_activity_bytes_per_second
            and sample.memory_available_change_bytes_per_second is not None
            and sample.memory_available_change_bytes_per_second < 0
        ):
            reasons.append("swap_activity_critical")
        if sample.cgroup_memory_oom_kill_delta and sample.cgroup_memory_oom_kill_delta > 0:
            reasons.append("cgroup_oom_kill")
        if sample.cgroup_memory_max_delta and sample.cgroup_memory_max_delta > 0:
            reasons.append("cgroup_memory_max")
        if sample.cgroup_memory_oom_delta and sample.cgroup_memory_oom_delta > 0:
            reasons.append("cgroup_memory_oom")
        return reasons

    def _soft_reasons(self, sample: ResourceSample) -> list[str]:
        t = self.thresholds
        reasons = []
        if sample.memory_available_bytes < t.warning_available_bytes:
            reasons.append("memory_available_low")
        if sample.swap_free_ratio < t.warning_swap_free_ratio:
            reasons.append("swap_free_low")
        memory_psi_trigger = sample.memory_psi_soft_trigger or t.pause_memory_psi
        io_psi_trigger = sample.io_psi_soft_trigger or t.pause_io_psi
        if (
            sample.memory_full_avg10 is not None
            and sample.memory_full_avg10 >= memory_psi_trigger
        ):
            reasons.append("memory_psi_high")
        if sample.io_full_avg10 is not None and sample.io_full_avg10 >= io_psi_trigger:
            reasons.append("io_psi_high")
        return reasons

    def _hard_recovered(self, sample: ResourceSample) -> bool:
        # Swap occupancy is sticky on Linux.  Recovery therefore requires RAM
        # headroom and no *current* swap hazard, but deliberately does not wait
        # for old swap pages or externally generated PSI to disappear.
        return (
            sample.memory_available_bytes >= self.thresholds.resume_available_bytes
            and not self._hard_reasons(sample)
        )

    def update(
        self,
        sample: ResourceSample | None,
        *,
        error: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        del error  # the stable public reason intentionally avoids exception text
        now = self.clock() if now is None else now
        self._sampled_at = datetime.now(timezone.utc).isoformat()

        if sample is None:
            self._sample = None
            self._failure_count += 1
            self._pause_count = 0
            self._recovery_started_at = None
            self._reset_stable_window()
            self._foreground_slow_count = 0
            self.reasons = ["resource_metrics_unavailable"]
            if self._failure_count >= self.thresholds.failure_samples:
                self._set_mode("critical")
                self._set_scale(0.0)
            elif self.controller_mode != "critical":
                self._set_mode("constrained")
                self._decrease_budget()
            return self.snapshot()

        self._sample = sample
        self._sampled_at = sample.sampled_at or self._sampled_at
        self._failure_count = 0
        hard_reasons = self._hard_reasons(sample)
        soft_reasons = self._soft_reasons(sample)
        if (
            sample.foreground_p95_ms is not None
            and sample.foreground_sample_count >= 5
            and sample.foreground_p95_ms
            > float(settings.resource_foreground_p95_limit_ms)
        ):
            self._foreground_slow_count += 1
        else:
            self._foreground_slow_count = 0
        if self._foreground_slow_count >= max(
            1,
            int(settings.resource_foreground_slow_samples),
        ):
            soft_reasons.append("foreground_latency_high")

        if self.controller_mode == "critical":
            self._set_scale(0.0)
            if hard_reasons:
                self._recovery_started_at = None
                self._reset_stable_window()
                self.reasons = hard_reasons
                return self.snapshot()
            if not self._hard_recovered(sample):
                self._recovery_started_at = None
                self.reasons = soft_reasons or ["recovery_threshold_not_met"]
                return self.snapshot()
            if self._recovery_started_at is None:
                self._recovery_started_at = now
            if now - self._recovery_started_at < self.thresholds.resume_seconds:
                self.reasons = ["recovery_stabilizing", *soft_reasons]
                return self.snapshot()
            self._pause_count = 0
            self._recovery_started_at = None
            self._set_mode("constrained" if soft_reasons else "normal")
            self.reasons = soft_reasons
            if soft_reasons:
                self._reset_stable_window()
                self._decrease_budget()
            else:
                # Leaving critical starts at the minimum safe trickle.  Further
                # additive recovery requires a new full stable minute.
                floor = max(0.01, min(1.0, float(settings.resource_budget_min_scale)))
                self._set_scale(floor)
                self._stable_since = now
                self._last_increase_at = now
            return self.snapshot()

        if hard_reasons:
            self._reset_stable_window()
            if any(
                reason in {
                    "cgroup_oom_kill",
                    "cgroup_memory_max",
                    "cgroup_memory_oom",
                }
                for reason in hard_reasons
            ):
                self._pause_count = self.thresholds.pause_samples
            else:
                self._pause_count += 1
            self.reasons = hard_reasons
            if self._pause_count >= self.thresholds.pause_samples:
                self._set_mode("critical")
                self._set_scale(0.0)
            else:
                self._set_mode("constrained")
                self._decrease_budget()
            return self.snapshot()

        self._pause_count = 0
        self._recovery_started_at = None
        if soft_reasons:
            self._reset_stable_window()
            self._set_mode("constrained")
            self.reasons = soft_reasons
            self._decrease_budget()
        else:
            self._set_mode("normal")
            self.reasons = []
            self._recover_budget_if_stable(now)
        return self.snapshot()

    def _profile_budget(self, profile: ResourceProfile) -> dict[str, Any]:
        sample = self._sample
        would_allow = profile.name == "light"
        reason = None
        if profile.name != "light":
            if self.controller_mode == "critical":
                reason = "controller_critical"
            elif sample is None:
                reason = "resource_metrics_unavailable"
            elif (
                sample.memory_available_bytes - profile.memory_reservation_bytes
                < self.thresholds.pause_available_bytes
            ):
                reason = "profile_memory_reserve"
            else:
                would_allow = True
        governance_mode = (
            "shadow" if str(settings.resource_governance_mode).lower() == "shadow" else "enforce"
        )
        # Shadow mode observes only the soft AIMD budget.  Absolute memory,
        # unreadable metrics and critical mode are hard safety gates in both
        # rollout modes.
        allowed = would_allow
        soft_enforced = profile_soft_budget_enforced(profile.name)
        effective_scale = (
            0.0
            if self.controller_mode == "critical"
            else 1.0
            if not soft_enforced
            else min(
                self.throughput_scale,
                max(0.01, min(1.0, float(settings.resource_governance_max_scale))),
            )
        )
        scale = effective_scale if allowed else 0.0
        token_scope = (
            "none"
            if profile.name == "light"
            else "network"
            if profile.name == "download_network"
            else "maintenance"
            if profile.name == "maintenance"
            else "disk"
        )
        reservation_capacity = (
            max(
                0,
                sample.memory_available_bytes - self.thresholds.pause_available_bytes,
            )
            if sample is not None
            else 0
        )
        return {
            "allowed": allowed,
            "would_allow": would_allow,
            "enforced": soft_enforced,
            "hard_gate_enforced": True,
            "soft_budget_enforced": soft_enforced,
            "reason": reason,
            "grant": "eligible" if allowed else "denied",
            "token_scope": token_scope,
            "memory_reservation_bytes": profile.memory_reservation_bytes,
            "reservation_capacity_bytes": reservation_capacity,
            "slice_seconds": (
                round(profile.base_slice_seconds * max(0.1, scale), 2)
                if profile.base_slice_seconds is not None
                else None
            ),
            "work_units": max(1, int(profile.base_work_units * max(0.1, scale))),
        }

    def snapshot(self) -> dict[str, Any]:
        sample = self._sample
        if sample is not None:
            hard_reasons = self._hard_reasons(sample)
            soft_reasons = self._soft_reasons(sample)
            if "foreground_latency_high" in self.reasons:
                soft_reasons.append("foreground_latency_high")
        elif self.reasons == ["resource_metrics_unavailable"]:
            hard_reasons = (
                ["resource_metrics_unavailable"]
                if self.controller_mode == "critical"
                else []
            )
            soft_reasons = (
                []
                if self.controller_mode == "critical"
                else ["resource_metrics_unavailable"]
            )
        else:
            hard_reasons = []
            soft_reasons = []
        governance_mode = (
            "shadow" if str(settings.resource_governance_mode).lower() == "shadow" else "enforce"
        )
        effective_scale = (
            0.0
            if self.controller_mode == "critical"
            else 1.0
            if governance_mode == "shadow"
            else min(
                self.throughput_scale,
                max(0.01, min(1.0, float(settings.resource_governance_max_scale))),
            )
        )
        read_bps = int(settings.resource_budget_base_read_mb_per_second * MIB * effective_scale)
        write_bps = int(settings.resource_budget_base_write_mb_per_second * MIB * effective_scale)
        budget_valid_for = max(10, settings.resource_pressure_snapshot_ttl_seconds)
        budget_valid_until_epoch = datetime.now(timezone.utc).timestamp() + budget_valid_for
        profile_budgets = {
            name: self._profile_budget(profile)
            for name, profile in RESOURCE_PROFILES.items()
        }
        return {
            "status": self.status,
            "controller_mode": self.controller_mode,
            "controller": {
                "mode": self.controller_mode,
                "legacy_status": self.status,
                "governance_mode": governance_mode,
                "enforced_profiles": sorted(_configured_enforced_profiles() or RESOURCE_PROFILES) if governance_mode == "enforce" else [],
                "algorithm": "aimd",
                "generation": self._generation,
                "throughput_scale": effective_scale,
                "computed_throughput_scale": self.throughput_scale,
                "effective_throughput_scale": effective_scale,
                "rollout_max_scale": float(settings.resource_governance_max_scale),
                "hard_gate_active": self.controller_mode == "critical",
                "psi_feedback_only": True,
                "hard_limits": {
                    "memory_available_bytes": self.thresholds.pause_available_bytes,
                    "swap_free_ratio": self.thresholds.critical_swap_free_ratio,
                    "active_swap_free_ratio": self.thresholds.pause_swap_free_ratio,
                    "swap_activity_bytes_per_second": (
                        self.thresholds.swap_activity_bytes_per_second
                    ),
                    "active_swap_requires_memory_decline": True,
                },
                "device_calibration": {
                    "memory_reserve_mode": self.thresholds.memory_reserve_mode,
                    "memory_total_bytes": (
                        sample.memory_total_bytes
                        if sample is not None
                        else self.thresholds.detected_memory_total_bytes
                    ),
                    "memory_reserve_ratio": self.thresholds.memory_reserve_ratio,
                    "memory_reserve_min_bytes": self.thresholds.memory_reserve_min_bytes,
                    "memory_reserve_max_bytes": self.thresholds.memory_reserve_max_bytes,
                    "memory_reserve_bytes": self.thresholds.pause_available_bytes,
                    "grantable_memory_capacity_bytes": (
                        max(
                            0,
                            sample.memory_available_bytes
                            - self.thresholds.pause_available_bytes,
                        )
                        if sample is not None
                        else None
                    ),
                    "warning_available_bytes": self.thresholds.warning_available_bytes,
                    "resume_available_bytes": self.thresholds.resume_available_bytes,
                    "source": (
                        "host_memtotal_bounded_ratio"
                        if self.thresholds.memory_reserve_mode == "auto"
                        else "fixed_configuration"
                    ),
                },
                "recovery_conditions": {
                    "scope": "auto_gallery_background_work_only",
                    "memory_available_at_least_bytes": self.thresholds.resume_available_bytes,
                    "hard_memory_reserve_bytes": self.thresholds.pause_available_bytes,
                    "swap_free_ratio_at_least": self.thresholds.critical_swap_free_ratio,
                    "active_swap_requires_memory_decline": True,
                    "project_cgroup_memory_events_stable": True,
                    "redis_writable": True,
                    "project_storage_available": True,
                    "stable_for_seconds": self.thresholds.resume_seconds,
                    "host_psi_is_soft_feedback_only": True,
                },
            },
            "reasons": list(dict.fromkeys(self.reasons)),
            "hard_reasons": list(dict.fromkeys(hard_reasons)),
            "soft_reasons": list(dict.fromkeys(soft_reasons)),
            "sampled_at": self._sampled_at,
            "memory": {
                "available_bytes": sample.memory_available_bytes if sample else None,
                "total_bytes": sample.memory_total_bytes if sample else None,
                "available_ratio": sample.memory_available_ratio if sample else None,
                "available_change_bytes_per_second": (
                    sample.memory_available_change_bytes_per_second if sample else None
                ),
            },
            "swap": {
                "free_bytes": sample.swap_free_bytes if sample else None,
                "total_bytes": sample.swap_total_bytes if sample else None,
                "free_ratio": sample.swap_free_ratio if sample else None,
                "in_bytes_per_second": sample.swap_in_bytes_per_second if sample else None,
                "out_bytes_per_second": sample.swap_out_bytes_per_second if sample else None,
                "activity_bytes_per_second": (
                    sample.swap_activity_bytes_per_second if sample else None
                ),
            },
            "psi": {
                "memory_full_avg10": sample.memory_full_avg10 if sample else None,
                "memory_full_avg60": sample.memory_full_avg60 if sample else None,
                "memory_full_avg300": sample.memory_full_avg300 if sample else None,
                "io_full_avg10": sample.io_full_avg10 if sample else None,
                "io_full_avg60": sample.io_full_avg60 if sample else None,
                "io_full_avg300": sample.io_full_avg300 if sample else None,
                "memory_soft_trigger": (
                    sample.memory_psi_soft_trigger if sample else None
                ),
                "io_soft_trigger": sample.io_psi_soft_trigger if sample else None,
                "feedback_only": True,
            },
            "baseline": {
                "window_seconds": PRESSURE_BASELINE_WINDOW_SECONDS,
                "sample_interval_seconds": PRESSURE_BASELINE_SAMPLE_SECONDS,
                "minimum_samples": PRESSURE_BASELINE_MIN_SAMPLES,
                "sample_count": sample.baseline_sample_count if sample else 0,
                "ready": bool(
                    sample
                    and sample.baseline_sample_count >= PRESSURE_BASELINE_MIN_SAMPLES
                ),
                "idle_only": True,
                "idle_observation": (
                    sample.baseline_idle_observation if sample else False
                ),
                "memory_psi": {
                    "median": sample.baseline_memory_psi_median if sample else None,
                    "p95": sample.baseline_memory_psi_p95 if sample else None,
                    "margin": float(settings.resource_baseline_memory_psi_margin),
                    "cap": float(settings.resource_baseline_memory_psi_cap),
                    "effective_trigger": (
                        sample.memory_psi_soft_trigger if sample else None
                    ),
                },
                "io_psi": {
                    "median": sample.baseline_io_psi_median if sample else None,
                    "p95": sample.baseline_io_psi_p95 if sample else None,
                    "margin": float(settings.resource_baseline_io_psi_margin),
                    "cap": float(settings.resource_baseline_io_psi_cap),
                    "effective_trigger": sample.io_psi_soft_trigger if sample else None,
                },
                "pathological_values_learned": False,
            },
            "trends": {
                "memory_available_change_bytes_per_second": (
                    sample.memory_available_change_bytes_per_second if sample else None
                ),
                "swap_in_bytes_per_second": (
                    sample.swap_in_bytes_per_second if sample else None
                ),
                "swap_out_bytes_per_second": (
                    sample.swap_out_bytes_per_second if sample else None
                ),
                "memory_psi_full": {
                    "avg10": sample.memory_full_avg10 if sample else None,
                    "avg60": sample.memory_full_avg60 if sample else None,
                    "avg300": sample.memory_full_avg300 if sample else None,
                },
                "io_psi_full": {
                    "avg10": sample.io_full_avg10 if sample else None,
                    "avg60": sample.io_full_avg60 if sample else None,
                    "avg300": sample.io_full_avg300 if sample else None,
                },
            },
            "foreground": {
                "p95_ms": sample.foreground_p95_ms if sample else None,
                "sample_count": sample.foreground_sample_count if sample else 0,
                "window_seconds": FOREGROUND_LATENCY_WINDOW_SECONDS,
                "soft_limit_ms": float(settings.resource_foreground_p95_limit_ms),
                "feedback_only": True,
            },
            "cgroup_memory_events": {
                "max": sample.cgroup_memory_max_events if sample else None,
                "oom": sample.cgroup_memory_oom_events if sample else None,
                "oom_kill": sample.cgroup_memory_oom_kill_events if sample else None,
                "max_delta": sample.cgroup_memory_max_delta if sample else None,
                "oom_delta": sample.cgroup_memory_oom_delta if sample else None,
                "oom_kill_delta": sample.cgroup_memory_oom_kill_delta if sample else None,
                "scope": "current_cgroup",
            },
            "budget": {
                "algorithm": "aimd",
                "governance_mode": governance_mode,
                "enforced_profiles": sorted(_configured_enforced_profiles() or RESOURCE_PROFILES) if governance_mode == "enforce" else [],
                "generation": self._generation,
                "valid_for_seconds": budget_valid_for,
                "valid_until": datetime.fromtimestamp(
                    budget_valid_until_epoch,
                    tz=timezone.utc,
                ).isoformat(),
                "valid_until_epoch": budget_valid_until_epoch,
                "throughput_scale": effective_scale,
                "computed_throughput_scale": self.throughput_scale,
                "effective_throughput_scale": effective_scale,
                "rollout_max_scale": float(settings.resource_governance_max_scale),
                "read_bytes_per_second": read_bps,
                "write_bytes_per_second": write_bps,
                "burst_seconds": 5,
                "profile_aliases": dict(RESOURCE_PROFILE_ALIASES),
                "profile_grants": {
                    name: {
                        "grant": value.get("grant"),
                        "allowed": value.get("allowed"),
                        "reason": value.get("reason"),
                        "token_scope": value.get("token_scope"),
                        "memory_reservation_bytes": value.get(
                            "memory_reservation_bytes"
                        ),
                    }
                    for name, value in profile_budgets.items()
                },
                "reservation": {
                    "mode": "single_disk_token_v1",
                    "hard_memory_floor_bytes": self.thresholds.pause_available_bytes,
                    "capacity_bytes": (
                        max(
                            0,
                            sample.memory_available_bytes
                            - self.thresholds.pause_available_bytes,
                        )
                        if sample is not None
                        else 0
                    ),
                    "active_leases": [],
                    "active_count": None,
                    "reserved_bytes": None,
                },
                "profiles": profile_budgets,
            },
        }


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
        reasons = tuple(snapshot.get("reasons") or [])
        if not source and "worker_cgroup_oom_kill" not in reasons:
            return None
        return (source, snapshot.get("sampled_at"), reasons)

    def hydrate_paused_once(self, snapshot: dict[str, Any] | None) -> None:
        """Hydrate once per process before the first local sample."""

        with self._lock:
            if self._hydrated:
                return
            self._hydrated = True
            if snapshot and snapshot.get("status") == "paused":
                self.state_machine.restore_paused(snapshot.get("reasons") or None)
                self._last_external_pause_marker = self._external_pause_marker(snapshot)

    def enforce_external_pause(self, snapshot: dict[str, Any]) -> None:
        """Apply a worker-originated hard latch to an already-hydrated monitor."""

        with self._lock:
            self._hydrated = True
            marker = self._external_pause_marker(snapshot)
            if self.state_machine.status != "paused" or (
                marker is not None and marker != self._last_external_pause_marker
            ):
                self.state_machine.restore_paused(snapshot.get("reasons") or None)
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
_publish_lock = threading.Lock()
_last_published_signature: tuple[Any, ...] | None = None
_last_published_at = 0.0
_last_publish_client_id: int | None = None


def _redis_client(redis_client=None):
    if redis_client is not None:
        return redis_client
    from app.services.redis_client import get_redis

    return get_redis()


def publish_resource_pressure_snapshot(snapshot: dict[str, Any], redis_client=None) -> bool:
    global _last_published_signature, _last_published_at, _last_publish_client_id
    try:
        client = _redis_client(redis_client)
        budget = snapshot.get("budget") or {}
        profiles = budget.get("profiles") or {}
        reservation = budget.get("reservation") or {}
        signature = (
            snapshot.get("status"),
            snapshot.get("controller_mode"),
            tuple(snapshot.get("reasons") or []),
            budget.get("generation"),
            budget.get("throughput_scale"),
            budget.get("governance_mode"),
            reservation.get("active_count"),
            reservation.get("reserved_bytes"),
            tuple(
                sorted(
                    str(value.get("token"))
                    for value in (reservation.get("active_leases") or [])
                    if isinstance(value, dict)
                )
            ),
            tuple(
                (name, value.get("allowed"), value.get("reason"))
                for name, value in sorted(profiles.items())
                if isinstance(value, dict)
            ),
        )
        now = time.monotonic()
        ttl = max(10, settings.resource_pressure_snapshot_ttl_seconds)
        client_id = id(client)
        with _publish_lock:
            changed = (
                signature != _last_published_signature
                or client_id != _last_publish_client_id
            )
            # Local sampling remains responsive at 5s, while stable Redis/AOF
            # traffic is capped at two snapshot writes per minute.
            heartbeat_due = now - _last_published_at >= min(30.0, ttl * 0.75)
            if not changed and not heartbeat_due:
                return True
        client.set(
            PRESSURE_SNAPSHOT_KEY,
            json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True),
            ex=ttl,
        )
        with _publish_lock:
            _last_published_signature = signature
            _last_published_at = now
            _last_publish_client_id = client_id
        if changed:
            try:
                client.publish(
                    RESOURCE_CONTROL_CHANNEL,
                    json.dumps(
                        {
                            "type": "resource_budget_changed",
                            "status": snapshot.get("status"),
                            "controller_mode": snapshot.get("controller_mode"),
                            "generation": budget.get("generation"),
                        },
                        separators=(",", ":"),
                    ),
                )
            except Exception:
                logger.debug("Unable to publish resource control event", exc_info=True)
        return True
    except Exception:
        logger.debug("Unable to publish resource pressure snapshot", exc_info=True)
        return False


def publish_external_resource_critical(
    reason: str,
    *,
    redis_client=None,
    source: str = "worker_cgroup",
) -> dict[str, Any]:
    """Merge a worker-local OOM kill into the shared hard safety latch."""

    snapshot = read_shared_resource_pressure_snapshot(redis_client=redis_client) or {}
    snapshot = deepcopy(snapshot)
    snapshot["status"] = "paused"
    snapshot["controller_mode"] = "critical"
    snapshot["sampled_at"] = datetime.now(timezone.utc).isoformat()
    snapshot["reasons"] = list(
        dict.fromkeys([*(snapshot.get("reasons") or []), reason])
    )
    snapshot["hard_reasons"] = list(
        dict.fromkeys([*(snapshot.get("hard_reasons") or []), reason])
    )
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
    _refresh_pressure_latch(snapshot, redis_client=redis_client)
    publish_resource_pressure_snapshot(snapshot, redis_client=redis_client)
    return snapshot


def read_shared_resource_pressure_snapshot(redis_client=None) -> dict[str, Any] | None:
    try:
        raw = _redis_client(redis_client).get(PRESSURE_SNAPSHOT_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("status") not in {"normal", "warning", "paused"}:
            return None
        value.setdefault("reasons", [])
        return value
    except Exception:
        return None


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


def _refresh_pressure_latch(snapshot: dict[str, Any], redis_client=None) -> None:
    """Persist paused state without adding a Redis/AOF write every 10 seconds."""

    try:
        client = _redis_client(redis_client)
        current = read_resource_pressure_latch(redis_client=client)
        ttl = client.ttl(PRESSURE_LATCH_KEY) if current else -2
        reasons_changed = bool(current) and current.get("reasons") != snapshot.get("reasons")
        if current is None or reasons_changed or ttl < PRESSURE_LATCH_TTL_SECONDS // 2:
            payload = {
                "status": "paused",
                "reasons": list(snapshot.get("reasons") or []),
                "sampled_at": snapshot.get("sampled_at"),
            }
            client.set(
                PRESSURE_LATCH_KEY,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
                ex=PRESSURE_LATCH_TTL_SECONDS,
            )
    except Exception:
        logger.debug("Unable to refresh resource pressure latch", exc_info=True)


def _clear_pressure_latch(redis_client=None) -> None:
    try:
        _redis_client(redis_client).delete(PRESSURE_LATCH_KEY)
    except Exception:
        logger.debug("Unable to clear resource pressure latch", exc_info=True)


def sample_and_publish_resource_pressure(redis_client=None) -> dict[str, Any]:
    # A short-lived shared snapshot is preferred because it contains the most
    # recent metrics.  The independent 24-hour latch survives backend downtime
    # and snapshot expiry, preventing a restart from resetting a paused guard.
    try:
        from app.services.heavy_io import collect_active_resource_leases

        active_leases = collect_active_resource_leases(redis_client=redis_client)
    except Exception as exc:
        active_leases = {
            "mode": "single_disk_token_v1",
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
    if not inherited or inherited.get("status") != "paused":
        inherited = read_resource_pressure_latch(redis_client=redis_client)
    if inherited and inherited.get("status") == "paused":
        _monitor.enforce_external_pause(inherited)
    else:
        _monitor.hydrate_paused_once(inherited)
    previous_status, snapshot = _monitor.sample_with_previous_status()
    reservation = snapshot.setdefault("budget", {}).setdefault("reservation", {})
    reservation.update(
        mode=active_leases.get("mode", "single_disk_token_v1"),
        active_leases=active_leases.get("active") or [],
        active_count=active_leases.get("active_count"),
        reserved_bytes=active_leases.get("reserved_bytes"),
        network_active=active_leases.get("network_active"),
        disk_active=active_leases.get("disk_active"),
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
        _clear_pressure_latch(redis_client=redis_client)
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
                "mode": "single_disk_token_v1",
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

        # Worker processes see their own cgroups, while this backend process may
        # not. Promote max/oom-kill boundaries as a hard shared latch even if
        # the originating worker crashed between HSET and snapshot publication.
        hard_memory_reason = (
            "worker_cgroup_oom_kill"
            if workers["cgroup_memory_events"]["oom_kill_delta"] > 0
            else "worker_cgroup_memory_max"
            if workers["cgroup_memory_events"]["max_delta"] > 0
            else None
        )
        if hard_memory_reason is not None:
            try:
                publish_external_resource_critical(
                    hard_memory_reason,
                    redis_client=client,
                    source="worker_health_aggregate",
                )
            except Exception:
                logger.warning(
                    "Unable to promote aggregated worker cgroup memory event",
                    exc_info=True,
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
