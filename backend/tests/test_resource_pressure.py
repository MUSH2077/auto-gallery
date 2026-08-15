import json

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
    ((2, 384), (4, 614), (8, 1228), (16, 1280)),
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
    assert machine.update(critical, now=20)["status"] == "paused"

    recovered = _sample()
    assert machine.update(recovered, now=30)["status"] == "paused"
    assert machine.update(recovered, now=89)["status"] == "paused"
    assert machine.update(recovered, now=90)["status"] == "normal"


def test_pressure_fails_closed_after_three_core_metric_failures():
    machine = ResourcePressureStateMachine()

    assert machine.update(None, error="OSError", now=0)["status"] == "warning"
    assert machine.update(None, error="OSError", now=10)["status"] == "warning"
    snapshot = machine.update(None, error="OSError", now=20)

    assert snapshot["status"] == "paused"
    assert snapshot["reasons"] == ["resource_metrics_unavailable"]


def test_sticky_swap_occupancy_constrains_but_does_not_pause_without_activity():
    machine = ResourcePressureStateMachine()
    sample = _sample(swap_free=GIB, memory_psi=None, io_psi=None)

    machine.update(sample, now=0)
    machine.update(sample, now=10)
    snapshot = machine.update(sample, now=20)

    assert snapshot["status"] == "warning"
    assert snapshot["controller_mode"] == "constrained"
    assert "swap_free_low" in snapshot["reasons"]
    assert snapshot["psi"]["memory_full_avg10"] is None


def test_active_swap_or_absolute_swap_floor_is_a_hard_gate():
    active = ResourcePressureStateMachine()
    sample = _sample(
        swap_free=GIB,
        swap_out=2 * 1024 ** 2,
        memory_change=-1024 ** 2,
    )
    for timestamp in (0, 10, 20):
        active_snapshot = active.update(sample, now=timestamp)
    assert active_snapshot["status"] == "paused"
    assert "swap_activity_critical" in active_snapshot["reasons"]

    exhausted = ResourcePressureStateMachine()
    sample = _sample(swap_free=int(0.05 * 6 * GIB))
    for timestamp in (0, 10, 20):
        exhausted_snapshot = exhausted.update(sample, now=timestamp)
    assert exhausted_snapshot["status"] == "paused"
    assert "swap_free_critical" in exhausted_snapshot["reasons"]


@pytest.mark.parametrize(
    ("field", "reason"),
    (
        ("cgroup_max", "cgroup_memory_max"),
        ("cgroup_oom", "cgroup_memory_oom"),
        ("cgroup_oom_kill", "cgroup_oom_kill"),
    ),
)
def test_new_cgroup_memory_events_fail_closed_immediately(field, reason):
    machine = ResourcePressureStateMachine()

    snapshot = machine.update(_sample(**{field: 1}), now=0)

    assert snapshot["controller_mode"] == "critical"
    assert snapshot["status"] == "paused"
    assert reason in snapshot["hard_reasons"]


def test_psi_is_soft_aimd_feedback_and_does_not_latch_pause():
    machine = ResourcePressureStateMachine()
    sample = _sample(memory_psi=8.0, io_psi=40.0)

    snapshots = [machine.update(sample, now=timestamp) for timestamp in (0, 10, 20, 30)]

    assert all(item["status"] == "warning" for item in snapshots)
    assert snapshots[-1]["controller_mode"] == "constrained"
    assert snapshots[-1]["budget"]["computed_throughput_scale"] == pytest.approx(0.1)
    assert snapshots[-1]["budget"]["effective_throughput_scale"] == pytest.approx(1.0)
    assert snapshots[-1]["budget"]["profiles"]["download_network"]["allowed"] is True


def test_aimd_additive_recovery_waits_a_full_stable_minute(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_budget_increase_step", 0.10)
    monkeypatch.setattr(
        pressure_module.settings,
        "resource_budget_increase_stable_seconds",
        60.0,
    )
    machine = ResourcePressureStateMachine()
    pressured = _sample(io_psi=40.0)
    for timestamp in (0, 5, 10, 15):
        snapshot = machine.update(pressured, now=timestamp)
    assert snapshot["budget"]["computed_throughput_scale"] == pytest.approx(0.1)

    assert machine.update(_sample(), now=20)["budget"]["computed_throughput_scale"] == pytest.approx(0.1)
    assert machine.update(_sample(), now=79)["budget"]["computed_throughput_scale"] == pytest.approx(0.1)
    assert machine.update(_sample(), now=80)["budget"]["computed_throughput_scale"] == pytest.approx(0.2)
    assert machine.update(_sample(), now=139)["budget"]["computed_throughput_scale"] == pytest.approx(0.2)
    assert machine.update(_sample(), now=140)["budget"]["computed_throughput_scale"] == pytest.approx(0.3)


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
    machine = ResourcePressureStateMachine()
    slow = _sample(foreground_p95=750.0, foreground_count=20)

    assert "foreground_latency_high" not in machine.update(slow, now=0)["reasons"]
    assert "foreground_latency_high" not in machine.update(slow, now=5)["reasons"]
    snapshot = machine.update(slow, now=10)

    assert snapshot["status"] == "warning"
    assert "foreground_latency_high" in snapshot["soft_reasons"]
    assert snapshot["hard_reasons"] == []


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
    machine = ResourcePressureStateMachine()
    snapshot = machine.update(_sample(available=int(1.55 * GIB)), now=0)

    assert snapshot["status"] == "normal"
    assert snapshot["budget"]["profiles"]["download_network"]["allowed"] is True
    assert snapshot["budget"]["profiles"]["video_derive"]["allowed"] is False
    assert snapshot["budget"]["profiles"]["video_derive"]["reason"] == "profile_memory_reserve"


def test_exact_profiles_and_aliases_are_exposed():
    snapshot = ResourcePressureStateMachine().update(_sample(), now=0)

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

    constrained = ResourcePressureStateMachine().update(
        _sample(io_psi=50.0),
        now=10,
    )
    assert constrained["budget"]["computed_throughput_scale"] == 0.5
    assert constrained["budget"]["effective_throughput_scale"] == 1.0


def test_profile_slice_limits_keep_shadow_at_base_and_scale_enforce(monkeypatch):
    from app.services import resource_pressure as pressure_module

    monkeypatch.setattr(pressure_module.settings, "resource_governance_mode", "shadow")
    shadow_snapshot = ResourcePressureStateMachine().update(
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
    enforce_snapshot = ResourcePressureStateMachine().update(
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
    snapshot = ResourcePressureStateMachine().update(_sample(), now=0)
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
    machine = ResourcePressureStateMachine()
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
    snapshot = ResourcePressureStateMachine().update(_sample(io_psi=50.0), now=0)

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


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.deleted = []

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
        self.ttls.pop(key, None)

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def info(self, section):
        if section == "memory":
            return {"used_memory": 80, "maxmemory": 100}
        if section == "errorstats":
            return {"errorstat_OOM": {"count": 7}}
        return {}


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
        ResourcePressureStateMachine().update(_sample(), now=0)
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
    from app.services import resource_pressure as pressure_module
    from app.services import settings as settings_module

    async def fake_pressure():
        return ResourcePressureStateMachine().update(_sample(), now=0)

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

    payload = await main._resource_pressure_health()

    assert payload["status"] == "warning"
    assert payload["controller_mode"] == "normal"
    assert payload["hard_reasons"] == []
    assert payload["reasons"] == ["redis_memory_high"]
    assert payload["redis"]["writable"] is True
    assert payload["queues"] == {"downloads": 2}
    assert payload["queue_activity"] == {}
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
        return ResourcePressureStateMachine().update(_sample(), now=0)

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
    }

    main._readiness_cache = None
    monkeypatch.setattr(main, "_service_readiness", redis_down)
    response = await main.ready()
    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "unavailable"
