#!/usr/bin/env python3
"""Fail-closed worker rollout gate for the public resource-health payload."""

from __future__ import annotations

import json
import sys
from typing import Any


def evaluate_recovery(health: Any) -> tuple[bool, str]:
    """Return whether controller-enforced hard recovery has completed.

    The summary intentionally contains only bounded controller state. Never
    echo the health payload: it is operational data and may grow new fields.
    """

    if not isinstance(health, dict):
        return False, "health payload is not an object"
    pressure = health.get("resource_pressure")
    if not isinstance(pressure, dict):
        return False, "resource_pressure is missing"
    controller = pressure.get("controller")
    if not isinstance(controller, dict):
        return False, "resource controller details are missing"

    status = pressure.get("status")
    mode = pressure.get("controller_mode")
    governance = controller.get("governance_mode")
    hard_gate = controller.get("hard_gate_active")
    hard_reasons = pressure.get("hard_reasons")
    trigger_reasons = pressure.get("trigger_reasons")
    remaining = pressure.get("recovery_remaining_seconds")

    failures: list[str] = []
    if status not in {"normal", "warning"}:
        failures.append(f"status={status!r}")
    if mode not in {"normal", "constrained"}:
        failures.append(f"controller_mode={mode!r}")
    if governance != "enforce":
        failures.append(f"governance_mode={governance!r}")
    if hard_gate is not False:
        failures.append(f"hard_gate_active={hard_gate!r}")
    if not isinstance(hard_reasons, list) or hard_reasons:
        count = len(hard_reasons) if isinstance(hard_reasons, list) else "missing"
        failures.append(f"hard_reasons={count}")
    if not isinstance(trigger_reasons, list) or trigger_reasons:
        count = len(trigger_reasons) if isinstance(trigger_reasons, list) else "missing"
        failures.append(f"trigger_reasons={count}")
    if remaining is not None:
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            failures.append("recovery_remaining_seconds=invalid")
        elif float(remaining) != 0.0:
            failures.append(f"recovery_remaining_seconds={float(remaining):.3f}")

    if failures:
        return False, ", ".join(failures)
    return True, f"status={status}, controller_mode={mode}, hard recovery cleared"


def main() -> int:
    try:
        health = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        print("waiting: health payload unavailable or invalid", file=sys.stderr)
        return 1

    ready, summary = evaluate_recovery(health)
    prefix = "ready" if ready else "waiting"
    print(f"{prefix}: {summary}", file=sys.stderr)
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
