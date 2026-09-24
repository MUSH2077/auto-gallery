"""Resource pressure thresholds, profiles, and hysteresis state machine."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import settings
from app.services.resource_pressure_sampling import (
    FOREGROUND_LATENCY_WINDOW_SECONDS,
    LOCAL_CGROUP_WARNING_SECONDS,
    ResourceSample,
    _parse_meminfo,
)

GIB = 1024 ** 3
MIB = 1024 ** 2
PRESSURE_BASELINE_WINDOW_SECONDS = 24 * 60 * 60
PRESSURE_BASELINE_SAMPLE_SECONDS = 60.0
PRESSURE_BASELINE_MIN_SAMPLES = 10

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
    maximum_bytes: int = 2560 * MIB,
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
        self._foreground_last_sample_generation: int | None = None
        self._sample: ResourceSample | None = None
        self._sampled_at: str | None = None
        self._trigger_reasons: list[str] = []
        self._snapshot_now: float | None = None
        self._local_cgroup_max_until = 0.0
        self._local_cgroup_oom_until = 0.0
        self._local_cgroup_max_delta = 0
        self._local_cgroup_oom_delta = 0

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
        self._trigger_reasons = list(self.reasons)
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
        self._snapshot_now = now
        self._sampled_at = datetime.now(timezone.utc).isoformat()
        if sample is not None:
            max_delta = max(0, int(sample.cgroup_memory_max_delta or 0))
            oom_delta = max(0, int(sample.cgroup_memory_oom_delta or 0))
            if max_delta:
                self._local_cgroup_max_until = now + LOCAL_CGROUP_WARNING_SECONDS
                self._local_cgroup_max_delta = max_delta
            if oom_delta:
                self._local_cgroup_oom_until = now + LOCAL_CGROUP_WARNING_SECONDS
                self._local_cgroup_oom_delta = oom_delta

        if sample is None:
            was_critical = self.controller_mode == "critical"
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
                if not was_critical:
                    self._trigger_reasons = ["resource_metrics_unavailable"]
            elif self.controller_mode != "critical":
                self._set_mode("constrained")
                self._decrease_budget()
            return self.snapshot()

        self._sample = sample
        self._sampled_at = sample.sampled_at or self._sampled_at
        self._failure_count = 0
        hard_reasons = self._hard_reasons(sample)
        soft_reasons = self._soft_reasons(sample)
        if sample.foreground_sample_count == 0:
            self._foreground_slow_count = 0
        elif (
            self._foreground_last_sample_generation is None
            or sample.foreground_sample_generation
            > self._foreground_last_sample_generation
        ):
            self._foreground_last_sample_generation = sample.foreground_sample_generation
            if (
                sample.foreground_p95_ms is not None
                and sample.foreground_sample_count
                >= max(30, int(settings.resource_foreground_min_samples))
                and sample.foreground_p95_ms
                > float(settings.resource_foreground_p95_limit_ms)
            ):
                self._foreground_slow_count += 1
            else:
                self._foreground_slow_count = 0
        if self._foreground_slow_count >= max(
            3,
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
            self._trigger_reasons = []
            if soft_reasons:
                self._reset_stable_window()
                self._decrease_budget()
            else:
                # Leaving critical starts at the minimum safe trickle.  Further
                # additive recovery requires a new full stable interval.
                floor = max(0.01, min(1.0, float(settings.resource_budget_min_scale)))
                self._set_scale(floor)
                self._stable_since = now
                self._last_increase_at = now
            return self.snapshot()

        if hard_reasons:
            self._reset_stable_window()
            if "cgroup_oom_kill" in hard_reasons:
                self._pause_count = self.thresholds.pause_samples
            else:
                self._pause_count += 1
            self.reasons = hard_reasons
            if self._pause_count >= self.thresholds.pause_samples:
                self._set_mode("critical")
                self._set_scale(0.0)
                self._trigger_reasons = list(hard_reasons)
            else:
                self._set_mode("constrained")
                self._decrease_budget()
            return self.snapshot()

        self._pause_count = 0
        self._recovery_started_at = None
        self._trigger_reasons = []
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
            else "ingest"
            if profile.name == "import_db"
            else "background"
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
        recovery_remaining_seconds = 0.0
        if self.controller_mode == "critical" and self._recovery_started_at is not None:
            snapshot_now = self.clock() if self._snapshot_now is None else self._snapshot_now
            recovery_remaining_seconds = round(
                max(
                    0.0,
                    self.thresholds.resume_seconds
                    - (snapshot_now - self._recovery_started_at),
                ),
                3,
            )
        snapshot_now = self.clock() if self._snapshot_now is None else self._snapshot_now
        local_cgroup_warning_reasons: list[str] = []
        if snapshot_now < self._local_cgroup_max_until:
            local_cgroup_warning_reasons.append("cgroup_memory_max")
        if snapshot_now < self._local_cgroup_oom_until:
            local_cgroup_warning_reasons.append("cgroup_memory_oom")
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
            "trigger_reasons": list(dict.fromkeys(self._trigger_reasons)),
            "hard_reasons": list(dict.fromkeys(hard_reasons)),
            "soft_reasons": list(dict.fromkeys(soft_reasons)),
            "recovery_remaining_seconds": recovery_remaining_seconds,
            "signal_scopes": {
                "memory": "host",
                "swap": "host",
                "psi": "host",
                "foreground": "backend_process",
                "cgroup_memory_events": "current_cgroup",
            },
            "local_cgroup_warnings": {
                "scope": "current_cgroup",
                "reasons": local_cgroup_warning_reasons,
                "max_delta": (
                    self._local_cgroup_max_delta
                    if snapshot_now < self._local_cgroup_max_until
                    else 0
                ),
                "oom_delta": (
                    self._local_cgroup_oom_delta
                    if snapshot_now < self._local_cgroup_oom_until
                    else 0
                ),
            },
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
                "sample_generation": (
                    sample.foreground_sample_generation if sample else 0
                ),
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
                    "mode": "dual_disk_lanes_v2",
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


