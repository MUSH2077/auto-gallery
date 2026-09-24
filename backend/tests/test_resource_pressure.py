import json
from dataclasses import replace

import pytest

from app.services.resource_pressure import (
    PRESSURE_LATCH_KEY,
    PRESSURE_SNAPSHOT_KEY,
    PressureBaselineWindow,
    PressureThresholds,
    ResourcePressureMonitor,
    ResourcePressureStateMachine,
    ResourceSample,
    automatic_memory_reserve_bytes,
    collect_redis_health,
    current_profile_slice_limits,
    profile_slice_cooldown_seconds,
    profile_slice_limits,
    publish_external_resource_critical,
    read_shared_resource_pressure_snapshot,
    sample_cgroup_memory_events,
    sample_cgroup_contribution,
    sample_resource_metrics,
    thresholds_from_settings,
)

GIB = 1024 ** 3


@pytest.mark.parametrize(
    ("total_gib", "expected_mib"),
    ((2, 384), (4, 614), (8, 1228), (16, 2457), (32, 2560)),
)
def test_automatic_memory_reserve_calibrates_across_device_sizes(total_gib, expected_mib):
    reserve = automatic_memory_reserve_bytes(total_gib * GIB)

    assert reserve // (1024 ** 2) == expected_mib


def test_fixed_memory_reserve_override_remains_available(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_memory_reserve_mode", "fixed")
    monkeypatch.setattr(pressure_module.settings, "resource_pressure_pause_available_mb", 768)

    thresholds = thresholds_from_settings(memory_total_bytes=16 * GIB)

    assert thresholds.memory_reserve_mode == "fixed"
    assert thresholds.pause_available_bytes == 768 * 1024 ** 2


def test_health_snapshot_exposes_device_reserve_and_grantable_capacity():
    reserve = 384 * 1024 ** 2
    machine = ResourcePressureStateMachine(
        PressureThresholds(
            warning_available_bytes=512 * 1024 ** 2,
            pause_available_bytes=reserve,
            resume_available_bytes=640 * 1024 ** 2,
            memory_reserve_mode="auto",
            memory_reserve_ratio=0.15,
            memory_reserve_min_bytes=reserve,
            memory_reserve_max_bytes=1280 * 1024 ** 2,
            detected_memory_total_bytes=2 * GIB,
        )
    )

    payload = machine.update(_sample(available=2 * GIB), now=0)
    calibration = payload["controller"]["device_calibration"]

    assert calibration["source"] == "host_memtotal_bounded_ratio"
    assert calibration["memory_reserve_bytes"] == reserve
    assert calibration["grantable_memory_capacity_bytes"] == 2 * GIB - reserve


def _sample(
    *,
    available=2 * GIB,
    swap_free=2 * GIB,
    memory_psi=0.0,
    io_psi=0.0,
    swap_in=0.0,
    swap_out=0.0,
    memory_change=0.0,
    foreground_p95=None,
    foreground_count=0,
    cgroup_max=0,
    cgroup_oom=0,
    cgroup_oom_kill=0,
):
    return ResourceSample(
        memory_total_bytes=8 * GIB,
        memory_available_bytes=available,
        swap_total_bytes=6 * GIB,
        swap_free_bytes=swap_free,
        memory_full_avg10=memory_psi,
        io_full_avg10=io_psi,
        swap_in_bytes_per_second=swap_in,
        swap_out_bytes_per_second=swap_out,
        memory_available_change_bytes_per_second=memory_change,
        foreground_p95_ms=foreground_p95,
        foreground_sample_count=foreground_count,
        cgroup_memory_max_delta=cgroup_max,
        cgroup_memory_oom_delta=cgroup_oom,
        cgroup_memory_oom_kill_delta=cgroup_oom_kill,
        sampled_at="2026-08-09T00:00:00+00:00",
    )


def test_pressure_requires_three_samples_and_stable_recovery():
    thresholds = PressureThresholds(resume_seconds=60)
    machine = ResourcePressureStateMachine(thresholds)
    critical = _sample(available=GIB)

    assert machine.update(critical, now=0)["status"] == "warning"
    assert machine.update(critical, now=10)["status"] == "warning"
    paused = machine.update(critical, now=20)
    assert paused["status"] == "paused"
    assert paused["trigger_reasons"] == ["memory_available_critical"]

    recovered = _sample()
    stabilizing = machine.update(recovered, now=30)
    assert stabilizing["status"] == "paused"
    assert stabilizing["trigger_reasons"] == ["memory_available_critical"]
    assert stabilizing["hard_reasons"] == []
    assert stabilizing["soft_reasons"] == []
    assert stabilizing["recovery_remaining_seconds"] == 60.0
    assert machine.update(recovered, now=89)["recovery_remaining_seconds"] == 1.0
    resumed = machine.update(recovered, now=90)
    assert resumed["status"] == "normal"
    assert resumed["trigger_reasons"] == []
    assert resumed["recovery_remaining_seconds"] == 0.0


def test_critical_latch_preserves_original_trigger_when_current_hazard_changes():
    machine = ResourcePressureStateMachine(PressureThresholds())
    memory_critical = _sample(available=GIB)
    for timestamp in (0, 10, 20):
        paused = machine.update(memory_critical, now=timestamp)

    swap_critical = _sample(swap_free=int(0.05 * 6 * GIB))
    changed = machine.update(swap_critical, now=30)

    assert paused["trigger_reasons"] == ["memory_available_critical"]
    assert changed["trigger_reasons"] == ["memory_available_critical"]
    assert changed["hard_reasons"] == ["swap_free_critical"]


def test_critical_latch_preserves_original_trigger_during_sampling_failures():
    machine = ResourcePressureStateMachine(PressureThresholds())
    memory_critical = _sample(available=GIB)
    for timestamp in (0, 10, 20):
        machine.update(memory_critical, now=timestamp)

    for timestamp in (30, 40, 50):
        failed = machine.update(None, error="OSError", now=timestamp)

    assert failed["trigger_reasons"] == ["memory_available_critical"]
    assert failed["hard_reasons"] == ["resource_metrics_unavailable"]


def test_pressure_fails_closed_after_three_core_metric_failures():
    machine = ResourcePressureStateMachine(PressureThresholds())

    assert machine.update(None, error="OSError", now=0)["status"] == "warning"
    assert machine.update(None, error="OSError", now=10)["status"] == "warning"
    snapshot = machine.update(None, error="OSError", now=20)

    assert snapshot["status"] == "paused"
    assert snapshot["reasons"] == ["resource_metrics_unavailable"]


def test_sticky_swap_occupancy_constrains_but_does_not_pause_without_activity():
    machine = ResourcePressureStateMachine(PressureThresholds())
    sample = _sample(swap_free=GIB, memory_psi=None, io_psi=None)

    machine.update(sample, now=0)
    machine.update(sample, now=10)
    snapshot = machine.update(sample, now=20)

    assert snapshot["status"] == "warning"
    assert snapshot["controller_mode"] == "constrained"
    assert "swap_free_low" in snapshot["reasons"]
    assert snapshot["psi"]["memory_full_avg10"] is None


def test_active_swap_or_absolute_swap_floor_is_a_hard_gate():
    active = ResourcePressureStateMachine(PressureThresholds())
    sample = _sample(
        swap_free=GIB,
        swap_out=2 * 1024 ** 2,
        memory_change=-1024 ** 2,
    )
    for timestamp in (0, 10, 20):
        active_snapshot = active.update(sample, now=timestamp)
    assert active_snapshot["status"] == "paused"
    assert "swap_activity_critical" in active_snapshot["reasons"]

    exhausted = ResourcePressureStateMachine(PressureThresholds())
    sample = _sample(swap_free=int(0.05 * 6 * GIB))
    for timestamp in (0, 10, 20):
        exhausted_snapshot = exhausted.update(sample, now=timestamp)
    assert exhausted_snapshot["status"] == "paused"
    assert "swap_free_critical" in exhausted_snapshot["reasons"]


@pytest.mark.parametrize("field", ("cgroup_max", "cgroup_oom"))
def test_cgroup_memory_events_do_not_change_global_pressure_state(field):
    machine = ResourcePressureStateMachine(PressureThresholds())

    snapshot = machine.update(_sample(**{field: 1}), now=0)

    assert snapshot["controller_mode"] == "normal"
    assert snapshot["status"] == "normal"
    assert snapshot["hard_reasons"] == []
    assert snapshot["soft_reasons"] == []


def test_backend_cgroup_warning_remains_local_and_observable_for_sixty_seconds():
    machine = ResourcePressureStateMachine(PressureThresholds())

    warning = machine.update(_sample(cgroup_max=2, cgroup_oom=1), now=0)
    still_warning = machine.update(_sample(), now=59)
    expired = machine.update(_sample(), now=60)

    assert warning["controller_mode"] == "normal"
    assert warning["reasons"] == []
    assert still_warning["local_cgroup_warnings"] == {
        "scope": "current_cgroup",
        "reasons": ["cgroup_memory_max", "cgroup_memory_oom"],
        "max_delta": 2,
        "oom_delta": 1,
    }
    assert expired["local_cgroup_warnings"]["reasons"] == []


def test_backend_cgroup_oom_kill_fails_closed_immediately():
    machine = ResourcePressureStateMachine(PressureThresholds())

    snapshot = machine.update(_sample(cgroup_oom_kill=1), now=0)

    assert snapshot["controller_mode"] == "critical"
    assert snapshot["status"] == "paused"
    assert snapshot["hard_reasons"] == ["cgroup_oom_kill"]


def test_psi_is_soft_aimd_feedback_and_does_not_latch_pause(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "shadow")
    machine = ResourcePressureStateMachine(PressureThresholds())
    sample = _sample(memory_psi=8.0, io_psi=40.0)

    snapshots = [machine.update(sample, now=timestamp) for timestamp in (0, 10, 20, 30)]

    assert all(item["status"] == "warning" for item in snapshots)
    assert snapshots[-1]["controller_mode"] == "constrained"
    assert snapshots[-1]["budget"]["computed_throughput_scale"] == pytest.approx(0.1)
    assert snapshots[-1]["budget"]["effective_throughput_scale"] == pytest.approx(1.0)
    assert snapshots[-1]["budget"]["profiles"]["download_network"]["allowed"] is True


def test_aimd_additive_recovery_waits_a_full_stable_thirty_seconds(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_budget_increase_step", 0.10)
    machine = ResourcePressureStateMachine(PressureThresholds())
    pressured = _sample(io_psi=40.0)
    for timestamp in (0, 5, 10, 15):
        snapshot = machine.update(pressured, now=timestamp)
    assert snapshot["budget"]["computed_throughput_scale"] == pytest.approx(0.1)

    assert machine.update(_sample(), now=20)["budget"]["computed_throughput_scale"] == pytest.approx(0.1)
    assert machine.update(_sample(), now=49)["budget"]["computed_throughput_scale"] == pytest.approx(0.1)
    assert machine.update(_sample(), now=50)["budget"]["computed_throughput_scale"] == pytest.approx(0.2)
    assert machine.update(_sample(), now=79)["budget"]["computed_throughput_scale"] == pytest.approx(0.2)
    assert machine.update(_sample(), now=80)["budget"]["computed_throughput_scale"] == pytest.approx(0.3)


def test_idle_baseline_calibrates_soft_psi_but_never_learns_pathology(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_baseline_io_psi_margin", 3.0)
    monkeypatch.setattr(pressure_module.settings, "resource_baseline_io_psi_cap", 30.0)
    now = {"value": 0.0}
    baseline = PressureBaselineWindow(clock=lambda: now["value"])
    baseline.hydrate(None)
    enriched = None
    for index in range(10):
        now["value"] = float((index + 1) * 60)
        enriched = baseline.enrich(
            _sample(memory_psi=1.0, io_psi=17.0),
            heavy_idle=True,
            thresholds=PressureThresholds(),
        )

    assert enriched is not None
    assert enriched.baseline_sample_count == 10
    assert enriched.baseline_io_psi_p95 == 17.0
    assert enriched.io_psi_soft_trigger == 20.0
    assert enriched.baseline_idle_observation is True

    now["value"] += 60
    pathological = baseline.enrich(
        _sample(memory_psi=9.0, io_psi=54.0),
        heavy_idle=True,
        thresholds=PressureThresholds(),
    )
    assert pathological.baseline_sample_count == 10
    assert pathological.baseline_io_psi_p95 == 17.0
    assert pathological.baseline_idle_observation is False

    now["value"] += 60
    busy = baseline.enrich(
        _sample(memory_psi=1.0, io_psi=1.0),
        heavy_idle=False,
        thresholds=PressureThresholds(),
    )
    assert busy.baseline_sample_count == 10
    assert busy.baseline_idle_observation is False


def test_foreground_p95_is_soft_feedback_only_after_consecutive_samples(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_foreground_slow_samples", 3)
    machine = ResourcePressureStateMachine(PressureThresholds())
    slow = _sample(foreground_p95=750.0, foreground_count=30)

    assert "foreground_latency_high" not in machine.update(
        replace(slow, foreground_sample_generation=1), now=0
    )["reasons"]
    assert "foreground_latency_high" not in machine.update(
        replace(slow, foreground_sample_generation=2), now=5
    )["reasons"]
    snapshot = machine.update(
        replace(slow, foreground_sample_generation=3), now=10
    )

    assert snapshot["status"] == "warning"
    assert "foreground_latency_high" in snapshot["soft_reasons"]
    assert snapshot["hard_reasons"] == []


def test_foreground_p95_ignores_a_rolling_window_with_fewer_than_thirty_requests(
    monkeypatch,
):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_foreground_slow_samples", 3)
    monkeypatch.setattr(pressure_module.settings, "resource_foreground_min_samples", 1)
    machine = ResourcePressureStateMachine(PressureThresholds())
    slow = _sample(foreground_p95=750.0, foreground_count=29)

    for generation, timestamp in enumerate((0, 5, 10), start=1):
        snapshot = machine.update(
            replace(slow, foreground_sample_generation=generation), now=timestamp
        )

    assert "foreground_latency_high" not in snapshot["soft_reasons"]


def test_foreground_p95_does_not_count_an_unchanged_sample_window_repeatedly(
    monkeypatch,
):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_foreground_slow_samples", 3)
    machine = ResourcePressureStateMachine(PressureThresholds())
    slow = _sample(foreground_p95=750.0, foreground_count=30)

    for timestamp in (0, 5, 10):
        snapshot = machine.update(slow, now=timestamp)

    assert "foreground_latency_high" not in snapshot["soft_reasons"]


def test_foreground_p95_clears_when_its_rolling_window_ages_out(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_foreground_slow_samples", 3)
    machine = ResourcePressureStateMachine(PressureThresholds())
    slow = _sample(foreground_p95=750.0, foreground_count=30)

    for generation, timestamp in enumerate((0, 5, 10), start=1):
        snapshot = machine.update(
            replace(slow, foreground_sample_generation=generation), now=timestamp
        )
    assert "foreground_latency_high" in snapshot["soft_reasons"]

    expired = _sample(
        foreground_p95=None,
        foreground_count=0,
    )
    snapshot = machine.update(
        replace(expired, foreground_sample_generation=3), now=15
    )

    assert "foreground_latency_high" not in snapshot["soft_reasons"]


def test_foreground_p95_always_requires_three_new_slow_evaluations(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_foreground_slow_samples", 1)
    machine = ResourcePressureStateMachine(PressureThresholds())
    slow = _sample(foreground_p95=750.0, foreground_count=30)

    for generation, timestamp in enumerate((1, 2), start=1):
        snapshot = machine.update(
            replace(slow, foreground_sample_generation=generation), now=timestamp
        )
        assert "foreground_latency_high" not in snapshot["soft_reasons"]

    snapshot = machine.update(
        replace(slow, foreground_sample_generation=3), now=3
    )
    assert "foreground_latency_high" in snapshot["soft_reasons"]


def test_foreground_latency_recorder_excludes_derivative_progress(monkeypatch):
    from app.services import resource_pressure as pressure_module

    from app.services import resource_pressure_sampling

    monkeypatch.setattr(resource_pressure_sampling, "_foreground_latencies", pressure_module.deque(maxlen=4096))
    monkeypatch.setattr(resource_pressure_sampling, "_foreground_latency_generation", 0)

    pressure_module.record_foreground_latency("/api/v1/works", 100.0)
    pressure_module.record_foreground_latency(
        "/api/v1/works/derivative-progress", 900.0
    )

    snapshot = pressure_module.foreground_latency_snapshot()
    assert snapshot["sample_count"] == 1
    assert snapshot["sample_generation"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "status_code", "recorded"),
    (
        ("GET", "/api/v1/works", 200, True),
        ("GET", "/api/v1/search?q=birds", 204, True),
        ("GET", "/api/v1/works", 500, False),
        ("GET", "/api/v1/works/derivative-progress", 200, False),
        ("POST", "/api/v1/works", 200, False),
        ("GET", "/api/v1/tags", 200, False),
    ),
)
async def test_foreground_latency_middleware_records_only_successful_interactive_gets(
    monkeypatch,
    method,
    path,
    status_code,
    recorded,
):
    from starlette.requests import Request
    from starlette.responses import Response

    import app.main as main
    from app.services import resource_pressure as pressure_module

    records = []
    monkeypatch.setattr(
        pressure_module,
        "record_foreground_latency",
        lambda recorded_path, duration_ms: records.append((recorded_path, duration_ms)),
    )
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )

    async def call_next(_request):
        return Response(status_code=status_code)

    await main.foreground_latency_feedback(request, call_next)

    assert bool(records) is recorded


@pytest.mark.asyncio
async def test_foreground_latency_middleware_does_not_record_cancelled_or_failed_requests(
    monkeypatch,
):
    from starlette.requests import Request

    import app.main as main
    from app.services import resource_pressure as pressure_module

    records = []
    monkeypatch.setattr(
        pressure_module,
        "record_foreground_latency",
        lambda recorded_path, duration_ms: records.append((recorded_path, duration_ms)),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/search",
            "query_string": b"",
            "headers": [],
        }
    )

    async def call_next(_request):
        raise RuntimeError("cancelled downstream")

    with pytest.raises(RuntimeError, match="cancelled downstream"):
        await main.foreground_latency_feedback(request, call_next)

    assert records == []


def test_critical_recovery_is_not_blocked_by_external_psi_or_sticky_swap():
    machine = ResourcePressureStateMachine(PressureThresholds(resume_seconds=60))
    critical = _sample(available=GIB)
    for timestamp in (0, 10, 20):
        machine.update(critical, now=timestamp)

    recovered = _sample(swap_free=GIB, memory_psi=9.0, io_psi=40.0)
    assert machine.update(recovered, now=30)["status"] == "paused"
    snapshot = machine.update(recovered, now=90)

    assert snapshot["status"] == "warning"
    assert snapshot["controller_mode"] == "constrained"
    assert "io_psi_high" in snapshot["reasons"]


def test_profile_reservation_keeps_absolute_memory_floor():
    machine = ResourcePressureStateMachine(PressureThresholds())
    snapshot = machine.update(_sample(available=int(1.55 * GIB)), now=0)

    assert snapshot["status"] == "normal"
    assert snapshot["budget"]["profiles"]["download_network"]["allowed"] is True
    assert snapshot["budget"]["profiles"]["video_derive"]["allowed"] is False
    assert snapshot["budget"]["profiles"]["video_derive"]["reason"] == "profile_memory_reserve"


def test_exact_profiles_and_aliases_are_exposed():
    snapshot = ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)

    assert {
        "download_network",
        "import_db",
        "image_derive",
        "video_derive",
        "search_index",
        "git_projection",
    }.issubset(snapshot["budget"]["profiles"])
    assert snapshot["budget"]["profile_aliases"]["download"] == "download_network"


def test_shadow_mode_keeps_hard_gate_but_does_not_enforce_soft_aimd(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "shadow")
    machine = ResourcePressureStateMachine(PressureThresholds(pause_samples=1))
    snapshot = machine.update(_sample(available=GIB), now=0)
    profile = snapshot["budget"]["profiles"]["video_derive"]

    assert snapshot["status"] == "paused"
    assert snapshot["budget"]["governance_mode"] == "shadow"
    assert profile["would_allow"] is False
    assert profile["allowed"] is False
    assert profile["enforced"] is False
    assert snapshot["budget"]["effective_throughput_scale"] == 0.0

    constrained = ResourcePressureStateMachine(PressureThresholds()).update(
        _sample(io_psi=50.0),
        now=10,
    )
    assert constrained["budget"]["computed_throughput_scale"] == 0.5
    assert constrained["budget"]["effective_throughput_scale"] == 1.0


def test_profile_slice_limits_keep_shadow_at_base_and_scale_enforce(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "shadow")
    shadow_snapshot = ResourcePressureStateMachine(PressureThresholds()).update(
        _sample(io_psi=50.0),
        now=0,
    )
    shadow = profile_slice_limits(
        shadow_snapshot,
        "import_db",
        max_work_units=25,
        max_slice_seconds=20.0,
    )

    assert shadow.allowed is True
    assert shadow.governance_mode == "shadow"
    assert shadow.work_units == 25
    assert shadow.slice_seconds == 20.0

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "enforce")
    enforce_snapshot = ResourcePressureStateMachine(PressureThresholds()).update(
        _sample(io_psi=50.0),
        now=0,
    )
    enforce = profile_slice_limits(
        enforce_snapshot,
        "import_db",
        max_work_units=25,
        max_slice_seconds=20.0,
    )

    assert enforce.allowed is True
    assert enforce.controller_mode == "constrained"
    assert enforce.effective_scale == pytest.approx(0.5)
    assert enforce.work_units == 12
    assert enforce.slice_seconds == pytest.approx(10.0)


def test_rollout_max_scale_caps_enforced_profiles_without_hiding_computed_budget(
    monkeypatch,
):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "enforce")
    monkeypatch.setattr(pressure_module.settings, "resource_governance_max_scale", 0.10)
    snapshot = ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)
    limits = profile_slice_limits(
        snapshot,
        "import_db",
        max_work_units=25,
        max_slice_seconds=20.0,
    )

    assert snapshot["budget"]["computed_throughput_scale"] == pytest.approx(1.0)
    assert snapshot["budget"]["effective_throughput_scale"] == pytest.approx(0.10)
    assert snapshot["budget"]["rollout_max_scale"] == pytest.approx(0.10)
    assert limits.effective_scale == pytest.approx(0.10)
    assert limits.work_units == 2
    assert limits.slice_seconds == pytest.approx(2.0)


def test_profile_slice_limits_never_zero_when_constrained_but_reject_critical(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "enforce")
    machine = ResourcePressureStateMachine(PressureThresholds())
    for timestamp in (0, 5, 10, 15):
        constrained_snapshot = machine.update(_sample(io_psi=50.0), now=timestamp)
    constrained = profile_slice_limits(constrained_snapshot, "import_db")

    assert constrained.allowed is True
    assert constrained.effective_scale == pytest.approx(0.1)
    assert constrained.work_units >= 1
    assert constrained.work_units == 2
    assert constrained.slice_seconds == pytest.approx(2.0)

    critical_snapshot = ResourcePressureStateMachine(
        PressureThresholds(pause_samples=1)
    ).update(_sample(available=GIB), now=20)
    critical = profile_slice_limits(critical_snapshot, "import_db")

    assert critical.allowed is False
    assert critical.controller_mode == "critical"
    assert critical.work_units == 0
    assert critical.slice_seconds == 0.0


def test_staged_profile_allowlist_enforces_only_selected_soft_budget(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "enforce")
    monkeypatch.setattr(
        pressure_module.settings,
        "resource_governance_enforced_profiles",
        "search_index",
    )
    snapshot = ResourcePressureStateMachine(PressureThresholds()).update(_sample(io_psi=50.0), now=0)

    search = profile_slice_limits(snapshot, "search_index", max_work_units=500)
    imported = profile_slice_limits(snapshot, "import_db", max_work_units=25)

    assert search.governance_mode == "enforce"
    # The 2,000-document NAS rebuild window scales to 1,000 here, then the
    # caller's explicit 500-unit cap remains authoritative.
    assert search.work_units == 500
    assert imported.governance_mode == "shadow"
    assert imported.work_units == 25
    assert profile_slice_cooldown_seconds(
        snapshot,
        workload="import_db",
        elapsed_seconds=10.0,
        jitter=False,
    ) == 0.0


def test_profile_slice_cooldown_is_enforce_only_bounded_and_not_a_hard_gate():
    base = {
        "status": "warning",
        "controller_mode": "constrained",
        "budget": {
            "governance_mode": "enforce",
            "effective_throughput_scale": 0.5,
        },
    }

    assert profile_slice_cooldown_seconds(
        base,
        elapsed_seconds=8.0,
        jitter=False,
    ) == 8.0
    assert profile_slice_cooldown_seconds(
        {
            **base,
            "budget": {
                **base["budget"],
                "effective_throughput_scale": 0.1,
            },
        },
        elapsed_seconds=20.0,
        max_seconds=30.0,
        jitter=False,
    ) == 30.0
    assert profile_slice_cooldown_seconds(
        {
            **base,
            "budget": {**base["budget"], "governance_mode": "shadow"},
        },
        elapsed_seconds=8.0,
        jitter=False,
    ) == 0.0
    assert profile_slice_cooldown_seconds(
        {
            **base,
            "status": "paused",
            "controller_mode": "critical",
            "budget": {
                **base["budget"],
                "effective_throughput_scale": 0.0,
            },
        },
        elapsed_seconds=8.0,
        jitter=False,
    ) == 0.0


@pytest.mark.asyncio
async def test_current_profile_slice_limits_returns_snapshot_without_waiting(monkeypatch):
    from app.services import resource_pressure as pressure_module

    snapshot = {
        "status": "normal",
        "controller_mode": "normal",
        "budget": {
            "governance_mode": "enforce",
            "effective_throughput_scale": 0.4,
            "profiles": {
                "search_index": {"allowed": True, "work_units": 200, "slice_seconds": 8.0},
            },
        },
    }

    async def _snapshot():
        return snapshot

    monkeypatch.setattr(pressure_module, "get_resource_pressure_snapshot", _snapshot)
    limits, observed = await current_profile_slice_limits(
        "search_index",
        max_work_units=500,
        max_slice_seconds=20.0,
    )

    assert observed is snapshot
    assert limits.allowed is True
    assert limits.work_units == 200
    assert limits.slice_seconds == 8.0


def test_sampler_parses_meminfo_and_optional_psi(tmp_path):
    (tmp_path / "pressure").mkdir()
    (tmp_path / "meminfo").write_text(
        "MemTotal: 8192 kB\nMemAvailable: 2048 kB\nSwapTotal: 4096 kB\nSwapFree: 1024 kB\n"
    )
    (tmp_path / "pressure" / "memory").write_text(
        "some avg10=1.00 avg60=0.00 avg300=0.00 total=1\n"
        "full avg10=2.50 avg60=0.00 avg300=0.00 total=1\n"
    )
    # Deliberately omit pressure/io: PSI absence is not a core-metric failure.

    sample = sample_resource_metrics(tmp_path)

    assert sample.memory_total_bytes == 8192 * 1024
    assert sample.memory_available_bytes == 2048 * 1024
    assert sample.memory_full_avg10 == 2.5
    assert sample.memory_full_avg60 == 0.0
    assert sample.memory_full_avg300 == 0.0
    assert sample.io_full_avg10 is None


def test_cgroup_memory_events_are_sampled_without_docker_socket(tmp_path):
    (tmp_path / "memory.events").write_text(
        "low 0\nhigh 2\nmax 7\noom 3\noom_kill 1\n"
    )

    assert sample_cgroup_memory_events(tmp_path) == {
        "max": 7,
        "oom": 3,
        "oom_kill": 1,
    }


def test_cgroup_v2_contribution_counters_and_psi_are_sampled(tmp_path):
    (tmp_path / "memory.current").write_text("1048576\n")
    (tmp_path / "memory.peak").write_text("2097152\n")
    (tmp_path / "memory.max").write_text("4194304\n")
    (tmp_path / "memory.events").write_text("max 2\noom 1\noom_kill 0\n")
    (tmp_path / "cpu.stat").write_text(
        "usage_usec 1000\nuser_usec 700\nsystem_usec 300\n"
        "nr_periods 10\nnr_throttled 2\nthrottled_usec 50\n"
    )
    (tmp_path / "io.stat").write_text(
        "8:0 rbytes=100 wbytes=200 rios=3 wios=4 dbytes=0 dios=0\n"
        "8:16 rbytes=50 wbytes=75 rios=1 wios=2 dbytes=0 dios=0\n"
    )
    for name in ("memory", "io", "cpu"):
        (tmp_path / f"{name}.pressure").write_text(
            "some avg10=1.00 avg60=2.00 avg300=3.00 total=1\n"
            "full avg10=0.50 avg60=1.00 avg300=1.50 total=1\n"
        )

    contribution = sample_cgroup_contribution(tmp_path)

    assert contribution["memory"] == {
        "current_bytes": 1048576,
        "peak_bytes": 2097152,
        "limit_bytes": 4194304,
    }
    assert contribution["cpu"]["nr_throttled"] == 2
    assert contribution["io"]["read_bytes"] == 150
    assert contribution["io"]["write_bytes"] == 275
    assert contribution["psi"]["io"]["full"]["avg10"] == 0.5
    assert contribution["memory_events"]["oom"] == 1


def test_cgroup_identity_fallback_is_namespaced_by_container_hostname(monkeypatch):
    from app.services import resource_pressure as pressure_module

    original_read_text = pressure_module.Path.read_text

    def unreadable_membership(path, *args, **kwargs):
        if path == pressure_module.Path("/proc/self/cgroup"):
            raise PermissionError("proc membership unavailable")
        return original_read_text(path, *args, **kwargs)

    hostnames = iter(("container-one", "container-two"))
    monkeypatch.setattr(pressure_module.Path, "read_text", unreadable_membership)
    monkeypatch.setattr(pressure_module.socket, "gethostname", lambda: next(hostnames))

    first = sample_cgroup_contribution()["cgroup_id"]
    second = sample_cgroup_contribution()["cgroup_id"]

    assert first == "container-one:/sys/fs/cgroup"
    assert second == "container-two:/sys/fs/cgroup"


class _FakeRedis:
    def __init__(self):
        self.now = 0.0
        self.values = {}
        self.hashes = {}
        self.sorted_sets = {}
        self.ttls = {}
        self.deleted = []
        self.before_eval = None

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **kwargs):
        self.values[key] = value
        if "ex" in kwargs:
            self.ttls[key] = kwargs["ex"]
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.sorted_sets.pop(key, None)
        self.ttls.pop(key, None)

    def advance(self, seconds):
        self.now += float(seconds)
        for key, remaining in list(self.ttls.items()):
            remaining -= float(seconds)
            if remaining > 0:
                self.ttls[key] = remaining
                continue
            self.values.pop(key, None)
            self.hashes.pop(key, None)
            self.sorted_sets.pop(key, None)
            self.ttls.pop(key, None)

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def expire(self, key, seconds):
        if (
            key not in self.values
            and key not in self.hashes
            and key not in self.sorted_sets
        ):
            return False
        self.ttls[key] = int(seconds)
        return True

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        values = self.hashes.setdefault(key, {})
        if mapping is not None:
            values.update(mapping)
        elif field is not None:
            values[field] = value
        return 1

    def eval(self, script, numkeys, *values):
        assert numkeys in {4, 5}
        if self.before_eval is not None:
            self.before_eval()

        if "HGETALL" in script:
            latch_key, ack_key, recovered_key, seen_key, event_key = values[:5]
            (
                expected_event,
                retention,
                _now,
                adoptable_event,
                adoptable_cgroup,
                adoptable_counter,
            ) = values[5:]
            raw_latch = self.get(latch_key)
            if raw_latch is None:
                return 0
            latch = json.loads(raw_latch)
            if not isinstance(latch, dict) or latch.get("status") != "paused":
                return 0
            current_event = str(
                (latch.get("controller") or {}).get("external_event_id") or ""
            )
            if current_event != expected_event:
                return 0
            if current_event:
                controller = latch.get("controller") or {}
                current_cgroup = controller.get("external_cgroup_id")
                try:
                    current_counter = int(controller["external_oom_kill_counter"])
                except (KeyError, TypeError, ValueError):
                    return 0
                acknowledged = int(self.hget(ack_key, current_cgroup) or 0)
                recovered = int(self.hget(recovered_key, current_cgroup) or 0)
                acknowledged_event = self.hget(event_key, current_cgroup)
                if (
                    acknowledged_event is None
                    and current_event == adoptable_event
                    and current_cgroup == adoptable_cgroup
                    and current_counter == int(adoptable_counter)
                    and acknowledged == current_counter
                    and recovered < current_counter
                ):
                    acknowledged_event = current_event
                    self.hset(event_key, current_cgroup, current_event)
                    self.expire(event_key, int(retention))
                if (
                    not isinstance(current_cgroup, str)
                    or not current_cgroup
                    or acknowledged != current_counter
                    or recovered >= current_counter
                    or acknowledged_event != current_event
                ):
                    return 0
                self.hashes.setdefault(recovered_key, {}).update(
                    self.hashes.get(ack_key, {})
                )
                self.expire(ack_key, int(retention))
                self.expire(recovered_key, int(retention))
                self.expire(seen_key, int(retention))
                self.expire(event_key, int(retention))
            self.delete(latch_key)
            return 1

        if numkeys == 4:
            ack_key, seen_key, recovered_key, event_key = values[:4]
            cgroup_id, counter, now, retention = values[4:]
            acknowledged = int(self.hget(ack_key, cgroup_id) or 0)
            if acknowledged < int(counter):
                return 0
            seen = self.sorted_sets.setdefault(seen_key, {})
            seen[cgroup_id] = float(now)
            self._prune_acknowledgments(
                ack_key,
                seen_key,
                recovered_key,
                event_key,
                float(now),
                float(retention),
            )
            self.expire(ack_key, int(retention))
            self.expire(seen_key, int(retention))
            self.expire(recovered_key, int(retention))
            self.expire(event_key, int(retention))
            recovered = int(self.hget(recovered_key, cgroup_id) or 0)
            return 2 if recovered >= int(counter) else 1

        ack_key, latch_key, seen_key, recovered_key, event_key = values[:5]
        (
            cgroup_id,
            counter,
            payload,
            ttl,
            now,
            retention,
            adoptable_event,
            adoptable_cgroup,
            adoptable_counter,
            adoption_checked,
        ) = values[5:]
        if seen_key not in self.sorted_sets and self.hashes.get(ack_key):
            self.sorted_sets[seen_key] = {
                identity: float(now) for identity in self.hashes[ack_key]
            }
            self.expire(ack_key, int(retention))
            self.expire(seen_key, int(retention))
        acknowledged = int(self.hget(ack_key, cgroup_id) or 0)
        if acknowledged >= int(counter):
            self.sorted_sets.setdefault(seen_key, {})[cgroup_id] = float(now)
            self._prune_acknowledgments(
                ack_key,
                seen_key,
                recovered_key,
                event_key,
                float(now),
                float(retention),
            )
            self.expire(ack_key, int(retention))
            self.expire(seen_key, int(retention))
            self.expire(recovered_key, int(retention))
            self.expire(event_key, int(retention))
            recovered = int(self.hget(recovered_key, cgroup_id) or 0)
            if recovered >= int(counter):
                return 2
            acknowledged_event = self.hget(event_key, cgroup_id)
            candidate_latch = json.loads(payload)
            candidate_controller = candidate_latch.get("controller")
            if (
                acknowledged_event is None
                and int(acknowledged) == int(counter)
                and candidate_controller.get("external_cgroup_id") == cgroup_id
                and int(candidate_controller.get("external_oom_kill_counter"))
                == int(counter)
                and isinstance(candidate_controller.get("external_event_id"), str)
                and candidate_controller["external_event_id"]
            ):
                acknowledged_event = candidate_controller["external_event_id"]
                self.hset(event_key, cgroup_id, acknowledged_event)
                self.expire(event_key, int(retention))
            raw_latch = self.get(latch_key)
            if raw_latch is not None:
                try:
                    latch = json.loads(raw_latch)
                except (TypeError, ValueError):
                    latch = None
                controller = latch.get("controller") if isinstance(latch, dict) else None
                current_cgroup = (
                    controller.get("external_cgroup_id")
                    if isinstance(controller, dict)
                    else None
                )
                try:
                    current_counter = int(controller["external_oom_kill_counter"])
                except (KeyError, TypeError, ValueError):
                    current_counter = None
                current_ack = int(self.hget(ack_key, current_cgroup) or 0)
                current_recovered = int(
                    self.hget(recovered_key, current_cgroup) or 0
                )
                current_event = self.hget(event_key, current_cgroup)
                legacy_event_missing = (
                    current_event is None
                    and isinstance(latch, dict)
                    and latch.get("status") == "paused"
                    and isinstance(controller, dict)
                    and isinstance(controller.get("external_event_id"), str)
                    and bool(controller["external_event_id"])
                    and isinstance(current_cgroup, str)
                    and bool(current_cgroup)
                    and current_counter is not None
                    and current_counter > 0
                    and current_ack == current_counter
                    and current_recovered < current_counter
                )
                if (
                    current_event is None
                    and legacy_event_missing
                    and isinstance(controller, dict)
                    and controller.get("external_event_id") == adoptable_event
                    and current_cgroup == adoptable_cgroup
                    and current_counter == int(adoptable_counter)
                    and current_ack == current_counter
                    and current_recovered < current_counter
                ):
                    current_event = controller["external_event_id"]
                    self.hset(event_key, current_cgroup, current_event)
                    self.expire(event_key, int(retention))
                if legacy_event_missing and current_event is None and not int(adoption_checked):
                    return 3
                represents_active_ack = (
                    isinstance(controller, dict)
                    and isinstance(controller.get("external_event_id"), str)
                    and bool(controller["external_event_id"])
                    and isinstance(current_cgroup, str)
                    and bool(current_cgroup)
                    and current_counter is not None
                    and current_counter > 0
                    and current_ack == current_counter
                    and current_recovered < current_counter
                    and current_event == controller.get("external_event_id")
                )
                if (
                    isinstance(latch, dict)
                    and latch.get("status") == "paused"
                    and represents_active_ack
                ):
                    return 0
            repaired_payload = json.loads(payload)
            if acknowledged_event:
                repaired_controller = repaired_payload["controller"]
                repaired_controller["external_event_id"] = acknowledged_event
                repaired_controller["external_cgroup_id"] = cgroup_id
                repaired_controller["external_oom_kill_counter"] = int(acknowledged)
            self.set(latch_key, json.dumps(repaired_payload), ex=int(ttl))
            return 1
        self.set(latch_key, payload, ex=int(ttl))
        self.hset(ack_key, cgroup_id, int(counter))
        candidate_controller = json.loads(payload).get("controller") or {}
        candidate_event = candidate_controller.get("external_event_id")
        if isinstance(candidate_event, str) and candidate_event:
            self.hset(event_key, cgroup_id, candidate_event)
        self.sorted_sets.setdefault(seen_key, {})[cgroup_id] = float(now)
        self._prune_acknowledgments(
            ack_key,
            seen_key,
            recovered_key,
            event_key,
            float(now),
            float(retention),
        )
        self.expire(ack_key, int(retention))
        self.expire(seen_key, int(retention))
        self.expire(recovered_key, int(retention))
        self.expire(event_key, int(retention))
        return 1

    def _prune_acknowledgments(
        self, ack_key, seen_key, recovered_key, event_key, now, retention
    ):
        seen = self.sorted_sets.setdefault(seen_key, {})
        cutoff = now - retention
        for stale_id, score in list(seen.items()):
            if score <= cutoff:
                self.hashes.get(ack_key, {}).pop(stale_id, None)
                self.hashes.get(recovered_key, {}).pop(stale_id, None)
                self.hashes.get(event_key, {}).pop(stale_id, None)
                seen.pop(stale_id, None)

    def info(self, section):
        if section == "memory":
            return {"used_memory": 80, "maxmemory": 100}
        if section == "errorstats":
            return {"errorstat_OOM": {"count": 7}}
        return {}


def test_backend_cgroup_warning_forces_shared_health_snapshot_publish(monkeypatch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    machine = ResourcePressureStateMachine(PressureThresholds())
    from app.services import resource_pressure_snapshot

    monkeypatch.setattr(resource_pressure_snapshot, "_last_published_signature", None)
    monkeypatch.setattr(resource_pressure_snapshot, "_last_published_at", 0.0)
    monkeypatch.setattr(resource_pressure_snapshot, "_last_publish_client_id", None)

    assert pressure_module.publish_resource_pressure_snapshot(
        machine.update(_sample(), now=0), redis
    )
    assert pressure_module.publish_resource_pressure_snapshot(
        machine.update(_sample(cgroup_max=1, cgroup_oom=1), now=1), redis
    )

    published = json.loads(redis.get(PRESSURE_SNAPSHOT_KEY))
    assert published["status"] == "normal"
    assert published["local_cgroup_warnings"]["reasons"] == [
        "cgroup_memory_max",
        "cgroup_memory_oom",
    ]


def test_shared_snapshot_and_redis_write_probe():
    redis = _FakeRedis()
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        {"status": "warning", "reasons": ["swap_free_low"]}
    ).encode()

    snapshot = read_shared_resource_pressure_snapshot(redis)
    health = collect_redis_health(redis)

    assert snapshot == {"status": "warning", "reasons": ["swap_free_low"]}
    assert health["usage_ratio"] == 0.8
    assert health["writable"] is True
    assert health["rejected_writes"] == 7
    assert health["oom_rejected_writes"] == 7
    assert health["application_rejected_enqueues"] == 0
    assert redis.deleted


def test_external_worker_oom_kill_closes_profiles_and_persists_latch():
    redis = _FakeRedis()
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)
    ).encode()

    snapshot = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-test",
    )

    assert snapshot["status"] == "paused"
    assert snapshot["controller_mode"] == "critical"
    assert snapshot["budget"]["effective_throughput_scale"] == 0.0
    assert snapshot["budget"]["profiles"]["download_network"]["allowed"] is False
    assert redis.get(PRESSURE_LATCH_KEY) is not None


def test_worker_oom_event_identity_depends_only_on_cgroup_and_counter():
    from app.services import resource_pressure as pressure_module

    first = pressure_module.cgroup_oom_kill_event_id("/docker/shared", 7)

    assert first == pressure_module.cgroup_oom_kill_event_id("/docker/shared", 7)
    assert first != pressure_module.cgroup_oom_kill_event_id("/docker/shared", 8)
    assert first != pressure_module.cgroup_oom_kill_event_id("/docker/other", 7)


def test_acknowledged_worker_oom_event_is_not_replayed_after_recovery():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)
    ).encode()
    event_id = pressure_module.cgroup_oom_kill_event_id("/docker/shared", 7)
    first = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-a",
        event_id=event_id,
        cgroup_id="/docker/shared",
        oom_kill_counter=7,
    )

    assert first is not None
    assert first["controller"]["external_event_id"] == event_id

    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=event_id,
    )
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=60)
    ).encode()
    replay = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-restarted",
        event_id=event_id,
        cgroup_id="/docker/shared",
        oom_kill_counter=7,
    )

    assert replay is None
    assert redis.get(PRESSURE_LATCH_KEY) is None

    next_event = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-restarted",
        event_id=pressure_module.cgroup_oom_kill_event_id("/docker/shared", 8),
        cgroup_id="/docker/shared",
        oom_kill_counter=8,
    )
    assert next_event is not None
    assert next_event["status"] == "paused"


def test_ack_without_latch_is_reasserted_until_controller_records_recovery():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    cgroup_id = "/docker/backend-outage"
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, 4)

    restarted = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-restarted",
        cgroup_id=cgroup_id,
        oom_kill_counter=4,
    )

    assert restarted is not None
    assert restarted["status"] == "paused"
    assert restarted["controller"]["hard_gate_active"] is True
    assert redis.get(PRESSURE_LATCH_KEY) is not None


@pytest.mark.parametrize(
    "stale_latch",
    (
        "not-json",
        json.dumps({"status": "normal", "controller": {}}),
    ),
)
def test_ack_with_non_authoritative_latch_is_repaired(stale_latch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    cgroup_id = "/docker/backend-corrupt-latch"
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, 4)
    redis.set(PRESSURE_LATCH_KEY, stale_latch, ex=60)

    repaired = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-restarted",
        cgroup_id=cgroup_id,
        oom_kill_counter=4,
    )

    persisted = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert repaired is not None
    assert repaired["status"] == "paused"
    assert persisted["status"] == "paused"
    assert persisted["controller"]["external_cgroup_id"] == cgroup_id
    assert persisted["controller"]["external_oom_kill_counter"] == 4


def test_ack_with_paused_wrong_event_identity_is_repaired_before_recovery():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    cgroup_id = "/docker/backend-unrecovered"
    counter = 4
    event_id = pressure_module.cgroup_oom_kill_event_id(cgroup_id, counter)
    stale_event_id = pressure_module.cgroup_oom_kill_event_id(
        "/docker/different",
        9,
    )
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, counter)
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "controller": {
                    "external_event_id": stale_event_id,
                    "external_cgroup_id": "/docker/different",
                    "external_oom_kill_counter": 9,
                },
            }
        ),
        ex=60,
    )

    repaired = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-restarted",
        event_id=event_id,
        cgroup_id=cgroup_id,
        oom_kill_counter=counter,
    )

    persisted = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert repaired is not None
    assert persisted["controller"]["external_event_id"] == event_id
    assert persisted["controller"]["external_cgroup_id"] == cgroup_id
    assert persisted["controller"]["external_oom_kill_counter"] == counter
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=stale_event_id,
    ) is False
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        cgroup_id,
    ) is None


def test_controller_recovery_marker_allows_acknowledged_restart_to_clear():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    monkey_event = pressure_module.cgroup_oom_kill_event_id("/docker/recovered", 3)
    first = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        cgroup_id="/docker/recovered",
        oom_kill_counter=3,
        event_id=monkey_event,
    )
    assert first is not None

    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=monkey_event,
    )
    restarted = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        cgroup_id="/docker/recovered",
        oom_kill_counter=3,
        event_id=monkey_event,
    )

    assert restarted is None
    assert redis.get(PRESSURE_LATCH_KEY) is None
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        "/docker/recovered",
    ) == 3


def test_new_worker_oom_preserves_trigger_from_expired_snapshot_latch():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "reasons": ["memory_available_critical"],
                "trigger_reasons": ["memory_available_critical"],
                "sampled_at": "2026-08-30T10:00:00+00:00",
                "controller": {},
            }
        ),
        ex=24 * 60 * 60,
    )
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/shared", 7)

    promoted = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-restarted",
        cgroup_id="/docker/shared",
        oom_kill_counter=8,
    )

    assert promoted is not None
    assert promoted["trigger_reasons"] == [
        "memory_available_critical",
        "worker_cgroup_oom_kill",
    ]
    persisted = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert persisted["trigger_reasons"] == [
        "memory_available_critical",
        "worker_cgroup_oom_kill",
    ]


def test_worker_oom_event_is_not_acknowledged_until_latch_persists():
    from app.services import resource_pressure as pressure_module

    class LatchFailsOnceRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.fail_latch = True

        def set(self, key, value, **kwargs):
            if key == PRESSURE_LATCH_KEY and self.fail_latch:
                self.fail_latch = False
                raise ConnectionError("redis unavailable")
            return super().set(key, value, **kwargs)

    redis = LatchFailsOnceRedis()
    event_id = pressure_module.cgroup_oom_kill_event_id("/docker/shared", 3)
    kwargs = {
        "redis_client": redis,
        "source": "worker-a",
        "event_id": event_id,
        "cgroup_id": "/docker/shared",
        "oom_kill_counter": 3,
    }

    with pytest.raises(RuntimeError, match="persist worker cgroup OOM latch"):
        publish_external_resource_critical("worker_cgroup_oom_kill", **kwargs)
    assert redis.hget(pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/shared") is None

    retried = publish_external_resource_critical("worker_cgroup_oom_kill", **kwargs)

    assert retried is not None
    assert redis.hget(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/shared"
    ) == 3


def test_concurrent_older_worker_oom_event_cannot_regress_acknowledgment():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)
    ).encode()
    newer_event_id = pressure_module.cgroup_oom_kill_event_id("/docker/shared", 8)

    def publish_newer_between_precheck_and_atomic_commit():
        redis.before_eval = None
        redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/shared", 8)
        redis.hset(
            pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
            "/docker/shared",
            newer_event_id,
        )
        redis.set(
            PRESSURE_LATCH_KEY,
            json.dumps(
                {
                    "status": "paused",
                    "reasons": ["worker_cgroup_oom_kill"],
                    "trigger_reasons": ["worker_cgroup_oom_kill"],
                    "controller": {
                        "external_event_id": newer_event_id,
                        "external_cgroup_id": "/docker/shared",
                        "external_oom_kill_counter": 8,
                    },
                }
            ),
            ex=60,
        )

    redis.before_eval = publish_newer_between_precheck_and_atomic_commit
    stale = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-stale",
        event_id=pressure_module.cgroup_oom_kill_event_id("/docker/shared", 7),
        cgroup_id="/docker/shared",
        oom_kill_counter=7,
    )

    assert stale is not None
    assert stale["controller"]["external_event_id"] == newer_event_id
    assert redis.hget(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/shared"
    ) == 8
    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert latch["controller"]["external_event_id"] == newer_event_id


def test_cross_cgroup_duplicate_cannot_replace_latest_active_oom_latch():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    stale_cgroup = "/docker/earlier-a"
    stale_counter = 7
    stale_event_id = pressure_module.cgroup_oom_kill_event_id(
        stale_cgroup,
        stale_counter,
    )
    latest_cgroup = "/docker/latest-b"
    latest_counter = 3
    latest_event_id = pressure_module.cgroup_oom_kill_event_id(
        latest_cgroup,
        latest_counter,
    )
    redis.hset(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY,
        stale_cgroup,
        stale_counter,
    )

    def publish_latest_between_read_and_atomic_commit():
        redis.before_eval = None
        redis.hset(
            pressure_module.CGROUP_OOM_ACK_HASH_KEY,
            latest_cgroup,
            latest_counter,
        )
        redis.hset(
            pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
            latest_cgroup,
            latest_event_id,
        )
        redis.set(
            PRESSURE_LATCH_KEY,
            json.dumps(
                {
                    "status": "paused",
                    "reasons": ["worker_cgroup_oom_kill"],
                    "trigger_reasons": ["worker_cgroup_oom_kill"],
                    "controller": {
                        "external_event_id": latest_event_id,
                        "external_cgroup_id": latest_cgroup,
                        "external_oom_kill_counter": latest_counter,
                    },
                }
            ),
            ex=60,
        )

    redis.before_eval = publish_latest_between_read_and_atomic_commit
    observed = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        source="worker-stale-a",
        event_id=stale_event_id,
        cgroup_id=stale_cgroup,
        oom_kill_counter=stale_counter,
    )

    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert observed is not None
    assert observed["controller"]["external_event_id"] == latest_event_id
    assert latch["controller"]["external_event_id"] == latest_event_id
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=stale_event_id,
    ) is False
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        latest_cgroup,
    ) is None


def test_stale_same_cgroup_candidate_repairs_latch_to_ack_high_water():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    cgroup_id = "/docker/same-cgroup"
    stale_counter = 7
    current_counter = 8
    stale_event_id = pressure_module.cgroup_oom_kill_event_id(
        cgroup_id,
        stale_counter,
    )
    current_event_id = pressure_module.cgroup_oom_kill_event_id(
        cgroup_id,
        current_counter,
    )
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, current_counter)
    redis.hset(
        "resource:cgroup:oom-kill-event-id:v1",
        cgroup_id,
        current_event_id,
    )
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "controller": {
                    "external_event_id": stale_event_id,
                    "external_cgroup_id": cgroup_id,
                    "external_oom_kill_counter": stale_counter,
                },
            }
        ),
        ex=60,
    )

    observed = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        event_id=stale_event_id,
        cgroup_id=cgroup_id,
        oom_kill_counter=stale_counter,
    )

    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert observed is not None
    assert latch["controller"]["external_event_id"] == current_event_id
    assert latch["controller"]["external_cgroup_id"] == cgroup_id
    assert latch["controller"]["external_oom_kill_counter"] == current_counter
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=stale_event_id,
    ) is False
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        cgroup_id,
    ) is None


def test_forged_cross_cgroup_latch_is_replaced_before_it_can_recover_ack():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    candidate_cgroup = "/docker/candidate-a"
    candidate_counter = 7
    candidate_event_id = pressure_module.cgroup_oom_kill_event_id(
        candidate_cgroup,
        candidate_counter,
    )
    current_cgroup = "/docker/current-b"
    current_counter = 3
    current_event_id = pressure_module.cgroup_oom_kill_event_id(
        current_cgroup,
        current_counter,
    )
    forged_event_id = "forged-current-event"
    for cgroup_id, counter, event_id in (
        (candidate_cgroup, candidate_counter, candidate_event_id),
        (current_cgroup, current_counter, current_event_id),
    ):
        redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, counter)
        redis.hset(
            "resource:cgroup:oom-kill-event-id:v1",
            cgroup_id,
            event_id,
        )
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "controller": {
                    "external_event_id": forged_event_id,
                    "external_cgroup_id": current_cgroup,
                    "external_oom_kill_counter": current_counter,
                },
            }
        ),
        ex=60,
    )

    publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        event_id=candidate_event_id,
        cgroup_id=candidate_cgroup,
        oom_kill_counter=candidate_counter,
    )

    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert latch["controller"]["external_event_id"] == candidate_event_id
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=forged_event_id,
    ) is False
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        current_cgroup,
    ) is None


def test_compare_clear_adopts_matching_legacy_latch_event_high_water():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    cgroup_id = "/docker/legacy-retired"
    counter = 4
    event_id = pressure_module.cgroup_oom_kill_event_id(cgroup_id, counter)
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, counter)
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "controller": {
                    "external_event_id": event_id,
                    "external_cgroup_id": cgroup_id,
                    "external_oom_kill_counter": counter,
                },
            }
        ),
        ex=60,
    )

    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=event_id,
    )
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        cgroup_id,
    ) == event_id
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        cgroup_id,
    ) == counter
    assert redis.ttl(pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY) > 0


def test_compare_clear_does_not_adopt_latch_changed_to_wrong_status_before_eval():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    cgroup_id = "/docker/legacy-clear-race"
    counter = 4
    event_id = pressure_module.cgroup_oom_kill_event_id(cgroup_id, counter)
    controller = {
        "external_event_id": event_id,
        "external_cgroup_id": cgroup_id,
        "external_oom_kill_counter": counter,
    }
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, counter)
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps({"status": "paused", "controller": controller}),
        ex=60,
    )

    def replace_latch_before_atomic_clear():
        redis.before_eval = None
        redis.set(
            PRESSURE_LATCH_KEY,
            json.dumps({"status": "normal", "controller": controller}),
            ex=60,
        )

    redis.before_eval = replace_latch_before_atomic_clear

    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=event_id,
    ) is False
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        cgroup_id,
    ) is None
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        cgroup_id,
    ) is None
    assert redis.get(PRESSURE_LATCH_KEY) is not None


def test_duplicate_adopts_matching_cross_cgroup_legacy_latch():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    candidate_cgroup = "/docker/current-a"
    candidate_counter = 7
    candidate_event = pressure_module.cgroup_oom_kill_event_id(
        candidate_cgroup,
        candidate_counter,
    )
    legacy_cgroup = "/docker/legacy-b"
    legacy_counter = 3
    legacy_event = pressure_module.cgroup_oom_kill_event_id(
        legacy_cgroup,
        legacy_counter,
    )
    redis.hset(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY,
        mapping={candidate_cgroup: candidate_counter, legacy_cgroup: legacy_counter},
    )
    redis.hset(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        candidate_cgroup,
        candidate_event,
    )
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "controller": {
                    "external_event_id": legacy_event,
                    "external_cgroup_id": legacy_cgroup,
                    "external_oom_kill_counter": legacy_counter,
                },
            }
        ),
        ex=60,
    )

    observed = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        event_id=candidate_event,
        cgroup_id=candidate_cgroup,
        oom_kill_counter=candidate_counter,
    )

    assert observed is not None
    assert observed["controller"]["external_event_id"] == legacy_event
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        legacy_cgroup,
    ) == legacy_event
    assert redis.ttl(pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY) > 0


def test_duplicate_does_not_adopt_latch_changed_to_wrong_status_before_eval():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    candidate_cgroup = "/docker/current-race-a"
    candidate_counter = 7
    candidate_event = pressure_module.cgroup_oom_kill_event_id(
        candidate_cgroup,
        candidate_counter,
    )
    legacy_cgroup = "/docker/legacy-race-b"
    legacy_counter = 3
    legacy_event = pressure_module.cgroup_oom_kill_event_id(
        legacy_cgroup,
        legacy_counter,
    )
    legacy_controller = {
        "external_event_id": legacy_event,
        "external_cgroup_id": legacy_cgroup,
        "external_oom_kill_counter": legacy_counter,
    }
    redis.hset(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY,
        mapping={candidate_cgroup: candidate_counter, legacy_cgroup: legacy_counter},
    )
    redis.hset(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        candidate_cgroup,
        candidate_event,
    )
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps({"status": "paused", "controller": legacy_controller}),
        ex=60,
    )

    def replace_latch_before_atomic_promotion():
        redis.before_eval = None
        redis.set(
            PRESSURE_LATCH_KEY,
            json.dumps({"status": "normal", "controller": legacy_controller}),
            ex=60,
        )

    redis.before_eval = replace_latch_before_atomic_promotion
    publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        event_id=candidate_event,
        cgroup_id=candidate_cgroup,
        oom_kill_counter=candidate_counter,
    )

    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert latch["status"] == "paused"
    assert latch["controller"]["external_event_id"] == candidate_event
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        legacy_cgroup,
    ) is None


def test_duplicate_adopts_legacy_latch_published_after_its_preread():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    candidate_cgroup = "/docker/current-a"
    candidate_counter = 7
    candidate_event = pressure_module.cgroup_oom_kill_event_id(
        candidate_cgroup,
        candidate_counter,
    )
    legacy_cgroup = "/docker/concurrent-legacy-b"
    legacy_counter = 3
    legacy_event = pressure_module.cgroup_oom_kill_event_id(
        legacy_cgroup,
        legacy_counter,
    )
    redis.hset(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY,
        candidate_cgroup,
        candidate_counter,
    )
    redis.hset(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        candidate_cgroup,
        candidate_event,
    )

    def publish_legacy_before_atomic_commit():
        redis.before_eval = None
        redis.hset(
            pressure_module.CGROUP_OOM_ACK_HASH_KEY,
            legacy_cgroup,
            legacy_counter,
        )
        redis.set(
            PRESSURE_LATCH_KEY,
            json.dumps(
                {
                    "status": "paused",
                    "controller": {
                        "external_event_id": legacy_event,
                        "external_cgroup_id": legacy_cgroup,
                        "external_oom_kill_counter": legacy_counter,
                    },
                }
            ),
            ex=60,
        )

    redis.before_eval = publish_legacy_before_atomic_commit
    observed = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        event_id=candidate_event,
        cgroup_id=candidate_cgroup,
        oom_kill_counter=candidate_counter,
    )

    assert observed is not None
    assert observed["controller"]["external_event_id"] == legacy_event
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        legacy_cgroup,
    ) == legacy_event


def test_duplicate_does_not_adopt_forged_legacy_latch_event():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    candidate_cgroup = "/docker/current-a"
    candidate_counter = 7
    candidate_event = pressure_module.cgroup_oom_kill_event_id(
        candidate_cgroup,
        candidate_counter,
    )
    legacy_cgroup = "/docker/forged-b"
    legacy_counter = 3
    redis.hset(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY,
        mapping={candidate_cgroup: candidate_counter, legacy_cgroup: legacy_counter},
    )
    redis.hset(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        candidate_cgroup,
        candidate_event,
    )
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "controller": {
                    "external_event_id": "forged-legacy-event",
                    "external_cgroup_id": legacy_cgroup,
                    "external_oom_kill_counter": legacy_counter,
                },
            }
        ),
        ex=60,
    )

    publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        event_id=candidate_event,
        cgroup_id=candidate_cgroup,
        oom_kill_counter=candidate_counter,
    )

    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert latch["controller"]["external_event_id"] == candidate_event
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        legacy_cgroup,
    ) is None


def test_cgroup_acknowledgments_expire_and_prune_inactive_identities(monkeypatch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/expired", 4)
    redis.hset(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY,
        "/docker/expired",
        pressure_module.cgroup_oom_kill_event_id("/docker/expired", 4),
    )
    redis.sorted_sets[pressure_module.CGROUP_OOM_ACK_SEEN_KEY] = {
        "/docker/expired": 1.0
    }
    monkeypatch.setattr(pressure_module.time, "time", lambda: 1_000_000.0)

    promoted = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        cgroup_id="/docker/current",
        oom_kill_counter=1,
    )

    assert promoted is not None
    assert redis.hget(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/expired"
    ) is None
    assert redis.hget(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/current"
    ) == 1
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY, "/docker/expired"
    ) is None
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY, "/docker/current"
    ) == pressure_module.cgroup_oom_kill_event_id("/docker/current", 1)
    assert redis.ttl(pressure_module.CGROUP_OOM_ACK_HASH_KEY) >= 24 * 60 * 60
    assert redis.ttl(pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY) >= 24 * 60 * 60


def test_active_cgroup_duplicate_touch_survives_retention_without_replay(monkeypatch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    monkeypatch.setattr(pressure_module.time, "time", lambda: redis.now)
    event_id = pressure_module.cgroup_oom_kill_event_id("/docker/long-lived", 2)
    assert publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        cgroup_id="/docker/long-lived",
        oom_kill_counter=2,
        event_id=event_id,
    )
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=event_id,
    )

    redis.advance(47 * 60 * 60)
    assert pressure_module.touch_cgroup_oom_kill_ack(
        redis,
        "/docker/long-lived",
        2,
    ) == "recovered"
    redis.advance(47 * 60 * 60)

    restarted = publish_external_resource_critical(
        "worker_cgroup_oom_kill",
        redis_client=redis,
        cgroup_id="/docker/long-lived",
        oom_kill_counter=2,
        event_id=event_id,
    )
    assert restarted is None
    assert redis.get(PRESSURE_LATCH_KEY) is None


def test_active_touch_prunes_only_identity_absent_beyond_retention(monkeypatch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    monkeypatch.setattr(pressure_module.time, "time", lambda: redis.now)
    for cgroup_id in ("/docker/active", "/docker/inactive"):
        event_id = pressure_module.cgroup_oom_kill_event_id(cgroup_id, 1)
        assert publish_external_resource_critical(
            "worker_cgroup_oom_kill",
            redis_client=redis,
            cgroup_id=cgroup_id,
            oom_kill_counter=1,
            event_id=event_id,
        )
    latest_event = pressure_module.cgroup_oom_kill_event_id("/docker/inactive", 1)
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=latest_event,
    )

    redis.advance(47 * 60 * 60)
    assert pressure_module.touch_cgroup_oom_kill_ack(
        redis,
        "/docker/active",
        1,
    ) == "recovered"
    redis.advance(2 * 60 * 60)
    assert pressure_module.touch_cgroup_oom_kill_ack(
        redis,
        "/docker/active",
        1,
    ) == "recovered"

    assert redis.hget(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/active"
    ) == 1
    assert redis.hget(
        pressure_module.CGROUP_OOM_ACK_HASH_KEY, "/docker/inactive"
    ) is None
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY, "/docker/inactive"
    ) is None
    assert redis.hget(
        pressure_module.CGROUP_OOM_EVENT_ID_HASH_KEY, "/docker/inactive"
    ) is None


def test_latch_refresh_preserves_external_event_metadata():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "reasons": ["worker_cgroup_oom_kill"],
                "trigger_reasons": ["worker_cgroup_oom_kill"],
                "sampled_at": "2026-08-30T10:00:00+00:00",
                "controller": {
                    "external_source": "worker-a",
                    "external_event_id": "stable-event",
                    "external_cgroup_id": "/docker/shared",
                    "external_oom_kill_counter": 7,
                },
            }
        ),
        ex=1,
    )
    state_machine_snapshot = {
        "status": "paused",
        "reasons": ["recovery_stabilizing"],
        "trigger_reasons": ["worker_cgroup_oom_kill"],
        "sampled_at": "2026-08-30T10:00:30+00:00",
        "controller": {"mode": "critical", "hard_gate_active": True},
    }

    assert pressure_module._refresh_pressure_latch(
        state_machine_snapshot, redis_client=redis
    )

    refreshed = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert refreshed["controller"] == {
        "external_source": "worker-a",
        "external_event_id": "stable-event",
        "external_cgroup_id": "/docker/shared",
        "external_oom_kill_counter": 7,
    }


def test_duplicate_aggregate_reports_from_shared_cgroup_are_promoted_once():
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)
    ).encode()
    stale = {
        "cgroup_id": "/docker/shared",
        "sampled_at": "2026-08-09T00:00:00+00:00",
        "memory_events": {"oom_kill": 7, "oom_kill_delta": 1},
    }
    current = {
        "cgroup_id": "/docker/shared",
        "sampled_at": "2026-08-09T00:00:30+00:00",
        "memory_events": {"oom_kill": 7, "oom_kill_delta": 1},
    }

    promoted = pressure_module.promote_worker_cgroup_oom_kills(
        [stale, current], redis_client=redis
    )
    latch = json.loads(redis.get(PRESSURE_LATCH_KEY))
    assert pressure_module._clear_pressure_latch(
        redis_client=redis,
        expected_external_event_id=latch["controller"]["external_event_id"],
    )
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=60)
    ).encode()
    replayed_after_restart = pressure_module.promote_worker_cgroup_oom_kills(
        [stale, current], redis_client=redis
    )

    assert promoted == 1
    assert replayed_after_restart == 0
    assert redis.get(PRESSURE_LATCH_KEY) is None


def test_external_worker_oom_kill_completes_stable_recovery_without_marker_resets():
    now = {"value": 0.0}
    healthy_sample = replace(_sample(), sampled_at=None)
    monitor = ResourcePressureMonitor(
        ResourcePressureStateMachine(
            PressureThresholds(resume_seconds=60),
            clock=lambda: now["value"],
        ),
        sampler=lambda: healthy_sample,
    )
    monitor.enforce_external_pause(
        {
            "status": "paused",
            "sampled_at": "2026-08-09T00:00:00+00:00",
            "reasons": ["worker_cgroup_oom_kill"],
            "trigger_reasons": ["worker_cgroup_oom_kill"],
            "controller": {
                "external_source": "worker-test",
                "external_event_id": "oom-event-1",
            },
        }
    )

    _, snapshot = monitor.sample_with_previous_status()
    assert snapshot["status"] == "paused"
    assert snapshot["recovery_remaining_seconds"] == 60.0

    for timestamp in (10, 20, 30, 40, 50):
        now["value"] = float(timestamp)
        monitor.enforce_external_pause(snapshot)
        _, snapshot = monitor.sample_with_previous_status()
        assert snapshot["status"] == "paused"

    now["value"] = 60.0
    monitor.enforce_external_pause(snapshot)
    _, snapshot = monitor.sample_with_previous_status()

    assert snapshot["status"] == "normal"
    assert snapshot["trigger_reasons"] == []


def test_only_worker_oom_kill_can_be_promoted_to_the_shared_latch():
    with pytest.raises(ValueError, match="worker_cgroup_oom_kill"):
        publish_external_resource_critical("worker_cgroup_memory_max", redis_client=_FakeRedis())


def test_new_external_worker_oom_resets_an_existing_recovery_window():
    thresholds = PressureThresholds(pause_samples=1, resume_seconds=60)
    machine = ResourcePressureStateMachine(thresholds)
    critical = _sample(available=GIB)
    recovered = _sample()
    assert machine.update(critical, now=0)["status"] == "paused"
    assert machine.update(recovered, now=1)["status"] == "paused"

    monitor = ResourcePressureMonitor(machine)
    monitor.enforce_external_pause(
        {
            "status": "paused",
            "sampled_at": "2026-08-09T00:00:30+00:00",
            "reasons": ["worker_cgroup_oom_kill"],
            "controller": {"external_source": "worker-test"},
        }
    )

    # The pre-existing window would have elapsed at t=61.  A new worker OOM is
    # a new hard event, so recovery starts over instead of opening immediately.
    assert machine.update(recovered, now=61)["status"] == "paused"


def test_redis_health_separates_server_oom_and_application_rejections():
    from app.services.queue_admission import QUEUE_REJECTION_COUNTER_KEY

    redis = _FakeRedis()
    redis.values[QUEUE_REJECTION_COUNTER_KEY] = b"3"

    health = collect_redis_health(redis)

    assert health["oom_rejected_writes"] == 7
    assert health["application_rejected_enqueues"] == 3
    assert health["rejected_writes"] == 10


def test_pause_latch_survives_process_restart_and_snapshot_expiry(monkeypatch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    now = {"value": 0.0}
    current_sample = {"value": _sample(available=GIB)}
    thresholds = PressureThresholds(resume_seconds=60)

    def clock():
        return now["value"]

    def sampler():
        return current_sample["value"]

    first_monitor = ResourcePressureMonitor(
        ResourcePressureStateMachine(thresholds, clock=clock),
        sampler=sampler,
    )
    monkeypatch.setattr(pressure_module, "_monitor", first_monitor)
    for timestamp in (0, 10, 20):
        now["value"] = timestamp
        snapshot = pressure_module.sample_and_publish_resource_pressure(redis)
    assert snapshot["status"] == "paused"
    assert redis.get(PRESSURE_LATCH_KEY) is not None

    # Simulate a new process after the short snapshot expired.  Its very first
    # critical sample inherits paused rather than restarting the 3-sample gate.
    redis.delete(PRESSURE_SNAPSHOT_KEY)
    restarted_critical = ResourcePressureMonitor(
        ResourcePressureStateMachine(thresholds, clock=clock),
        sampler=sampler,
    )
    monkeypatch.setattr(pressure_module, "_monitor", restarted_critical)
    now["value"] = 30
    assert pressure_module.sample_and_publish_resource_pressure(redis)["status"] == "paused"

    # Restart once more into already-recovered metrics.  The first sample must
    # remain paused and the latch may clear only after the full 60 seconds.
    redis.delete(PRESSURE_SNAPSHOT_KEY)
    current_sample["value"] = _sample()
    restarted_recovery = ResourcePressureMonitor(
        ResourcePressureStateMachine(thresholds, clock=clock),
        sampler=sampler,
    )
    monkeypatch.setattr(pressure_module, "_monitor", restarted_recovery)
    now["value"] = 100
    assert pressure_module.sample_and_publish_resource_pressure(redis)["status"] == "paused"
    assert redis.get(PRESSURE_LATCH_KEY) is not None
    now["value"] = 159
    assert pressure_module.sample_and_publish_resource_pressure(redis)["status"] == "paused"
    assert redis.get(PRESSURE_LATCH_KEY) is not None
    now["value"] = 160
    assert pressure_module.sample_and_publish_resource_pressure(redis)["status"] == "normal"
    assert redis.get(PRESSURE_LATCH_KEY) is None


def test_invalid_external_latch_cannot_publish_normal_when_compare_clear_fails(
    monkeypatch,
):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    now = {"value": 0.0}
    cgroup_id = "/docker/unseen-high-water"
    stale_event_id = pressure_module.cgroup_oom_kill_event_id(cgroup_id, 7)
    current_event_id = pressure_module.cgroup_oom_kill_event_id(cgroup_id, 8)
    redis.hset(pressure_module.CGROUP_OOM_ACK_HASH_KEY, cgroup_id, 8)
    redis.hset(
        "resource:cgroup:oom-kill-event-id:v1",
        cgroup_id,
        current_event_id,
    )
    redis.set(
        PRESSURE_LATCH_KEY,
        json.dumps(
            {
                "status": "paused",
                "reasons": ["worker_cgroup_oom_kill"],
                "trigger_reasons": ["worker_cgroup_oom_kill"],
                "controller": {
                    "external_source": "worker-test",
                    "external_event_id": stale_event_id,
                    "external_cgroup_id": cgroup_id,
                    "external_oom_kill_counter": 7,
                },
            }
        ),
        ex=60,
    )
    monitor = ResourcePressureMonitor(
        ResourcePressureStateMachine(
            PressureThresholds(resume_seconds=60),
            clock=lambda: now["value"],
        ),
        sampler=lambda: _sample(),
    )
    monkeypatch.setattr(pressure_module, "_monitor", monitor)

    assert pressure_module.sample_and_publish_resource_pressure(redis)["status"] == "paused"
    now["value"] = 60.0
    snapshot = pressure_module.sample_and_publish_resource_pressure(redis)

    assert snapshot["status"] == "paused"
    assert json.loads(redis.get(PRESSURE_SNAPSHOT_KEY))["status"] == "paused"
    assert redis.get(PRESSURE_LATCH_KEY) is not None
    assert redis.hget(
        pressure_module.CGROUP_OOM_RECOVERED_HASH_KEY,
        cgroup_id,
    ) is None


def test_pause_latch_overrides_an_older_nonpaused_snapshot(monkeypatch):
    from app.services import resource_pressure as pressure_module

    redis = _FakeRedis()
    redis.values[PRESSURE_SNAPSHOT_KEY] = json.dumps(
        {"status": "warning", "reasons": ["memory_available_low"]}
    ).encode()
    redis.values[PRESSURE_LATCH_KEY] = json.dumps(
        {"status": "paused", "reasons": ["memory_available_critical"]}
    ).encode()
    redis.ttls[PRESSURE_LATCH_KEY] = 86400
    monitor = ResourcePressureMonitor(
        ResourcePressureStateMachine(PressureThresholds(resume_seconds=60), clock=lambda: 30),
        sampler=lambda: _sample(available=GIB),
    )
    monkeypatch.setattr(pressure_module, "_monitor", monitor)

    snapshot = pressure_module.get_resource_pressure_snapshot_sync(redis)

    assert snapshot["status"] == "paused"
    assert snapshot["reasons"] == ["memory_available_critical"]


@pytest.mark.asyncio
async def test_health_resource_shape_is_additive(monkeypatch):
    import app.main as main
    from app.services import device_profile
    from app.services import resource_pressure as pressure_module
    from app.services import settings as settings_module

    async def fake_pressure():
        return ResourcePressureStateMachine(PressureThresholds()).update(
            _sample(cgroup_max=2, cgroup_oom=1), now=0
        )

    async def fake_defaults(_session):
        return {"download_concurrency": 4}

    monkeypatch.setattr(pressure_module, "get_resource_pressure_snapshot", fake_pressure)
    monkeypatch.setattr(
        pressure_module,
        "collect_redis_health",
        lambda: {
            "used_memory_bytes": 80,
            "maxmemory_bytes": 100,
            "usage_ratio": 0.8,
            "writable": True,
            "rejected_writes": 0,
        },
    )
    monkeypatch.setattr(
        pressure_module,
        "collect_queue_worker_health",
        lambda: (
            {"downloads": 2},
            {
                "rq": {},
                "supervisors": {
                    "host:downloads": {
                        "queues": ["downloads"],
                        "effective_concurrency": 1,
                    },
                    "host:imports": {"queues": ["imports"]},
                    "host:operations": {"queues": ["operations"]},
                    "host:scheduled": {"queues": ["scheduled"]},
                },
            },
        ),
    )
    monkeypatch.setattr(settings_module, "get_download_defaults", fake_defaults)
    monkeypatch.setattr(
        device_profile,
        "current_device_profile",
        lambda: device_profile.resolve_device_profile(
            "standard",
            memory_total_bytes=16 * GIB,
            cpu_count=6,
        ),
    )

    payload = await main._resource_pressure_health()

    assert payload["status"] == "warning"
    assert payload["controller_mode"] == "normal"
    assert payload["hard_reasons"] == []
    assert payload["reasons"] == ["redis_memory_high"]
    assert payload["redis"]["writable"] is True
    assert payload["queues"] == {"downloads": 2}
    assert payload["queue_activity"] == {}
    assert payload["signal_scopes"] == {
        "memory": "host",
        "swap": "host",
        "psi": "host",
        "foreground": "backend_process",
        "cgroup_memory_events": "current_cgroup",
    }
    assert payload["trigger_reasons"] == []
    assert payload["recovery_remaining_seconds"] == 0.0
    assert payload["local_cgroup_warnings"] == {
        "scope": "current_cgroup",
        "reasons": ["cgroup_memory_max", "cgroup_memory_oom"],
        "max_delta": 2,
        "oom_delta": 1,
    }
    assert payload["download_concurrency"] == {
        "configured": 4,
        "cap": 1,
        "device_profile": "standard",
        "effective": 1,
        "desired_effective": 1,
        "restart_required": False,
    }


@pytest.mark.asyncio
async def test_health_resource_reports_worker_circuit_and_redis_write_failure(monkeypatch):
    import app.main as main
    from app.services import resource_pressure as pressure_module

    async def fake_pressure():
        return ResourcePressureStateMachine(PressureThresholds()).update(_sample(), now=0)

    monkeypatch.setattr(pressure_module, "get_resource_pressure_snapshot", fake_pressure)
    monkeypatch.setattr(
        pressure_module,
        "collect_redis_health",
        lambda: {
            "used_memory_bytes": 95,
            "maxmemory_bytes": 100,
            "usage_ratio": 0.95,
            "writable": False,
            "rejected_writes": 2,
        },
    )
    monkeypatch.setattr(
        pressure_module,
        "collect_queue_worker_health",
        lambda: (
            {},
            {
                "rq": {},
                "supervisors": {
                    "host:downloads": {
                        "state": "circuit_open",
                        "worker_count": 0,
                        "queues": ["downloads"],
                    },
                    "host:imports": {
                        "state": "running",
                        "worker_count": 1,
                        "queues": ["imports"],
                    },
                    "host:operations": {
                        "state": "running",
                        "worker_count": 1,
                        "queues": ["operations"],
                    },
                    "host:scheduled": {
                        "state": "running",
                        "worker_count": 1,
                        "queues": ["scheduled"],
                    },
                },
            },
        ),
    )

    payload = await main._resource_pressure_health()

    assert payload["status"] == "paused"
    assert payload["controller_mode"] == "critical"
    assert payload["hard_reasons"] == ["redis_unwritable"]
    assert payload["reasons"] == [
        "redis_unwritable",
        "redis_memory_critical",
        "worker_circuit_open",
    ]


@pytest.mark.asyncio
async def test_ready_allows_meili_degradation_but_requires_writable_redis(monkeypatch):
    import app.main as main

    async def meili_down():
        return {"postgres": "up", "redis": "up", "meilisearch": "down"}

    async def redis_down():
        return {"postgres": "up", "redis": "down", "meilisearch": "up"}

    main._readiness_cache = None
    monkeypatch.setattr(main, "_service_readiness", meili_down)
    response = await main.ready()
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "degraded",
        "services": {"postgres": "up", "redis": "up", "meilisearch": "down"},
        "build_revision": main.settings.build_revision,
    }

    main._readiness_cache = None
    monkeypatch.setattr(main, "_service_readiness", redis_down)
    response = await main.ready()
    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "unavailable"
