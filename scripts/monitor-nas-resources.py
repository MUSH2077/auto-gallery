#!/usr/bin/env python3
"""Emit JSONL samples for the 8GB-NAS resource acceptance window.

Run this on the Docker host. It intentionally stays outside the application
containers so the backend never needs the privileged Docker socket. On cgroup
v2 hosts each sample includes memory.current, memory.peak and memory.events for
every auto-gallery container, plus Docker OOM/restart state and host pressure.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_HEALTH_URL = "http://127.0.0.1:8818/api/v1/system/health"
_previous_sample: dict[str, Any] | None = None
_compose_command = ["docker", "compose"]


def _command_json(command: list[str]) -> Any:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def _read_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def _read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, raw = line.split(maxsplit=1)
            values[key] = int(raw)
    except (OSError, ValueError):
        return {}
    return values


def _container_cgroup(pid: int) -> Path | None:
    if pid <= 0:
        return None
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
            hierarchy, controllers, relative = line.split(":", 2)
            if hierarchy == "0" and controllers == "":
                candidate = CGROUP_ROOT / relative.lstrip("/")
                return candidate if candidate.exists() else None
    except (OSError, ValueError):
        return None
    return None


def _host_meminfo() -> dict[str, int | float | None]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    return {
        "memory_total_bytes": values.get("MemTotal"),
        "memory_available_bytes": values.get("MemAvailable"),
        "swap_total_bytes": swap_total,
        "swap_free_bytes": swap_free,
        "swap_free_ratio": (
            swap_free / swap_total
            if swap_total and swap_free is not None
            else 1.0 if swap_total == 0 else None
        ),
    }


def _psi(path: Path) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if not fields or fields[0] not in {"some", "full"}:
                continue
            result[fields[0]] = {
                key: float(value)
                for key, value in (item.split("=", 1) for item in fields[1:])
                if key in {"avg10", "avg60", "avg300"}
            }
    except (OSError, ValueError):
        return {}
    return result


def _io_stat(path: Path) -> dict[str, int]:
    totals: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            for field in line.split()[1:]:
                key, raw = field.split("=", 1)
                totals[key] = totals.get(key, 0) + int(raw)
    except (OSError, ValueError):
        return {}
    return totals


def _application_health(url: str | None) -> dict[str, Any] | None:
    if not url:
        return None
    try:
        with urlopen(url, timeout=2.0) as response:  # noqa: S310 - local operator URL
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None
    pressure = payload.get("resource_pressure") or {}
    business = payload.get("business") or {}
    return {
        "status": payload.get("status"),
        "controller_mode": pressure.get("controller_mode"),
        "legacy_pressure_status": pressure.get("status"),
        "hard_reasons": pressure.get("hard_reasons", []),
        "soft_reasons": pressure.get("soft_reasons", pressure.get("reasons", [])),
        "budget": pressure.get("budget", {}),
        "outboxes": business.get("outboxes", {}),
        "queue_activity": business.get("queue_activity", {}),
    }


def _compose_container_ids() -> list[str]:
    result = subprocess.run(
        [*_compose_command, "ps", "-q"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return [line for line in result.stdout.splitlines() if line]


def _container_samples() -> list[dict[str, Any]]:
    container_ids = _compose_container_ids()
    if not container_ids:
        return []
    inspected = _command_json(["docker", "inspect", *container_ids])
    samples: list[dict[str, Any]] = []
    for container in inspected:
        state = container.get("State") or {}
        host_config = container.get("HostConfig") or {}
        pid = int(state.get("Pid") or 0)
        cgroup = _container_cgroup(pid)
        events = _read_key_values(cgroup / "memory.events") if cgroup else {}
        cpu = _read_key_values(cgroup / "cpu.stat") if cgroup else {}
        io = _io_stat(cgroup / "io.stat") if cgroup else {}
        samples.append(
            {
                "name": str(container.get("Name") or "").lstrip("/"),
                "running": bool(state.get("Running")),
                "oom_killed": bool(state.get("OOMKilled")),
                "restart_count": int(container.get("RestartCount") or 0),
                "memory_limit_bytes": int(host_config.get("Memory") or 0),
                "memory_swap_limit_bytes": int(host_config.get("MemorySwap") or 0),
                "memory_current_bytes": _read_int(cgroup / "memory.current") if cgroup else None,
                "memory_peak_bytes": _read_int(cgroup / "memory.peak") if cgroup else None,
                "memory_events": events,
                "cpu_stat": cpu,
                "io_stat": io,
                "memory_pressure": _psi(cgroup / "memory.pressure") if cgroup else {},
                "io_pressure": _psi(cgroup / "io.pressure") if cgroup else {},
            }
        )
    return sorted(samples, key=lambda item: item["name"])


def _with_rates(sample: dict[str, Any]) -> dict[str, Any]:
    global _previous_sample
    now = time.monotonic()
    previous = _previous_sample
    _previous_sample = {"clock": now, "sample": sample}
    if previous is None or now <= previous["clock"]:
        return sample
    elapsed = now - previous["clock"]
    old_containers = {
        item["name"]: item
        for item in previous["sample"]["auto_gallery"]["containers"]
    }
    for container in sample["auto_gallery"]["containers"]:
        old = old_containers.get(container["name"], {})
        old_io = old.get("io_stat") or {}
        old_cpu = old.get("cpu_stat") or {}
        io = container.get("io_stat") or {}
        cpu = container.get("cpu_stat") or {}
        container["rates"] = {
            "read_bytes_per_second": max(0, io.get("rbytes", 0) - old_io.get("rbytes", 0)) / elapsed,
            "write_bytes_per_second": max(0, io.get("wbytes", 0) - old_io.get("wbytes", 0)) / elapsed,
            "cpu_usage_cores": max(0, cpu.get("usage_usec", 0) - old_cpu.get("usage_usec", 0)) / (elapsed * 1_000_000),
        }
    sample["sample_interval_seconds"] = elapsed
    sample["auto_gallery"]["read_bytes_per_second"] = sum(
        (item.get("rates") or {}).get("read_bytes_per_second", 0.0)
        for item in sample["auto_gallery"]["containers"]
    )
    sample["auto_gallery"]["write_bytes_per_second"] = sum(
        (item.get("rates") or {}).get("write_bytes_per_second", 0.0)
        for item in sample["auto_gallery"]["containers"]
    )
    return sample


def collect_sample(*, health_url: str | None = DEFAULT_HEALTH_URL) -> dict[str, Any]:
    containers = _container_samples()
    current_values = [item["memory_current_bytes"] for item in containers]
    peak_values = [item["memory_peak_bytes"] for item in containers]
    sample = {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            **_host_meminfo(),
            "memory_pressure": _psi(Path("/proc/pressure/memory")),
            "io_pressure": _psi(Path("/proc/pressure/io")),
        },
        "auto_gallery": {
            "container_count": len(containers),
            "memory_current_bytes": sum(value for value in current_values if value is not None),
            "memory_peak_bytes_sum": sum(value for value in peak_values if value is not None),
            "oom_kill_count": sum(
                int((item.get("memory_events") or {}).get("oom_kill", 0))
                for item in containers
            ),
            "restart_count": sum(item["restart_count"] for item in containers),
            "containers": containers,
        },
        "application": _application_health(health_url),
    }
    return _with_rates(sample)


def main() -> int:
    global _compose_command
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between samples")
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="total seconds to monitor; zero emits one sample",
    )
    parser.add_argument(
        "--health-url",
        default=DEFAULT_HEALTH_URL,
        help="unauthenticated system health URL; empty disables application sampling",
    )
    parser.add_argument("--project-name", help="Compose project name to sample")
    parser.add_argument("--env-file", help="Compose environment file")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Compose file; repeat for overrides",
    )
    args = parser.parse_args()
    _compose_command = ["docker", "compose", "--project-directory", str(ROOT)]
    if args.project_name:
        _compose_command.extend(["-p", args.project_name])
    if args.env_file:
        _compose_command.extend(["--env-file", args.env_file])
    for compose_file in args.compose_file:
        _compose_command.extend(["-f", compose_file])
    interval = max(1.0, args.interval)
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while True:
            print(
                json.dumps(
                    collect_sample(health_url=args.health_url or None),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                flush=True,
            )
            if deadline is None or time.monotonic() + interval > deadline:
                break
            time.sleep(interval)
    except (FileNotFoundError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
