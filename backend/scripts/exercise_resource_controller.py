#!/usr/bin/env python3
"""Deterministic hard/soft controller acceptance without stressing the host."""

from __future__ import annotations

import json

from app.services.resource_pressure import (
    GIB,
    PressureThresholds,
    ResourcePressureStateMachine,
    ResourceSample,
)


def sample(*, available: float = 2.0, swap_ratio: float = 0.8, io_psi: float = 0.0) -> ResourceSample:
    swap_total = 6 * GIB
    return ResourceSample(
        memory_total_bytes=8 * GIB,
        memory_available_bytes=int(available * GIB),
        swap_total_bytes=swap_total,
        swap_free_bytes=int(swap_total * swap_ratio),
        memory_full_avg10=0.0,
        io_full_avg10=io_psi,
    )


def main() -> int:
    machine = ResourcePressureStateMachine(
        PressureThresholds(pause_samples=3, failure_samples=3, resume_seconds=60)
    )
    normal = machine.update(sample(), now=0)
    constrained = machine.update(sample(io_psi=30), now=5)
    # PSI remains soft feedback even after three samples.
    machine.update(sample(io_psi=30), now=10)
    soft_only = machine.update(sample(io_psi=30), now=15)
    machine.update(sample(available=1.0), now=20)
    machine.update(sample(available=1.0), now=25)
    critical = machine.update(sample(available=1.0), now=30)
    assert normal["controller_mode"] == "normal"
    assert constrained["controller_mode"] == "constrained"
    assert soft_only["controller_mode"] == "constrained"
    assert critical["controller_mode"] == "critical"
    assert critical["status"] == "paused"
    print(json.dumps({"normal": normal, "soft_only": soft_only, "critical": critical}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
