#!/usr/bin/env python3
"""Run a bounded command with project-local resource safety gates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time
from typing import Any

MIB = 1024 * 1024
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
def _meminfo() -> dict[str, int | float]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("SwapTotal", 0)
    free = values.get("SwapFree", 0)
    return {
        "memory_available_bytes": values.get("MemAvailable", 0),
        "swap_free_bytes": free,
        "swap_total_bytes": total,
        "swap_free_ratio": free / total if total else 1.0,
    }


def _psi_full(path: str) -> float:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields and fields[0] == "full":
            for field in fields[1:]:
                if field.startswith("avg10="):
                    return float(field.split("=", 1)[1])
    raise RuntimeError(f"full avg10 unavailable in {path}")


def _swap_pages() -> int:
    values: dict[str, int] = {}
    for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] in {"pswpin", "pswpout"}:
            values[fields[0]] = int(fields[1])
    return values.get("pswpin", 0) + values.get("pswpout", 0)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile from no samples")
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


def _load_probe_state(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _containers(project: str) -> list[dict[str, Any]]:
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not ids:
        return []
    payload = json.loads(
        subprocess.run(
            ["docker", "inspect", *ids], check=True, capture_output=True, text=True
        ).stdout
    )
    rows = []
    for item in payload:
        state = item.get("State") or {}
        pid = int(state.get("Pid") or 0)
        events: dict[str, int] = {}
        memory_current = None
        memory_peak = None
        io_totals: dict[str, int] = {}
        if pid:
            try:
                relative = next(
                    line.split(":", 2)[2]
                    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                    if line.startswith("0::")
                )
                event_path = Path("/sys/fs/cgroup") / relative.lstrip("/") / "memory.events"
                events = {
                    key: int(value)
                    for key, value in (
                        line.split() for line in event_path.read_text().splitlines()
                    )
                }
                cgroup = event_path.parent
                for name, target in (("memory.current", "current"), ("memory.peak", "peak")):
                    try:
                        value = int((cgroup / name).read_text().strip())
                    except (OSError, ValueError):
                        value = None
                    if target == "current":
                        memory_current = value
                    else:
                        memory_peak = value
                try:
                    for line in (cgroup / "io.stat").read_text().splitlines():
                        for field in line.split()[1:]:
                            key, value = field.split("=", 1)
                            io_totals[key] = io_totals.get(key, 0) + int(value)
                except (OSError, ValueError):
                    io_totals = {}
            except (OSError, StopIteration, ValueError):
                events = {}
        rows.append(
            {
                "name": str(item.get("Name") or "").lstrip("/"),
                "restart_count": int(item.get("RestartCount") or 0),
                "oom_killed": bool(state.get("OOMKilled")),
                "memory_events": events,
                "memory_current_bytes": memory_current,
                "memory_peak_bytes": memory_peak,
                "io_stat": io_totals,
            }
        )
    return rows


def _event_signature(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int, int]]:
    return {
        row["name"]: (
            int((row["memory_events"] or {}).get("max", 0)),
            int((row["memory_events"] or {}).get("oom_kill", 0)),
            int(row["restart_count"]),
        )
        for row in rows
    }


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument(
        "--policy",
        choices=("build-functional", "load-soak", "deploy-core"),
        default="load-soak",
    )
    parser.add_argument("--memory-floor-mib", type=int, default=1536)
    parser.add_argument("--swap-free-ratio", type=float, default=0.25)
    parser.add_argument("--baseline-file")
    parser.add_argument("--baseline-seconds", type=int, default=300)
    parser.add_argument("--memory-psi-delta", type=float, default=10.0)
    parser.add_argument("--io-psi-delta", type=float, default=20.0)
    parser.add_argument("--memory-drop-mib", type=int, default=256)
    parser.add_argument("--swap-activity-mib-per-minute", type=float, default=64.0)
    parser.add_argument("--application-write-mib-per-second", type=float, default=8.0)
    parser.add_argument("--foreground-p95-seconds", type=float, default=1.5)
    parser.add_argument("--probe-state")
    parser.add_argument("--consecutive", type=int, default=6)
    parser.add_argument("--preflight-samples", type=int, default=3)
    parser.add_argument("--preflight-timeout-seconds", type=int, default=300)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    baseline_path = (
        Path(args.baseline_file)
        if args.baseline_file
        else report.with_name("host-baseline.json")
    )
    host_baseline: dict[str, Any]
    try:
        host_baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if host_baseline.get("schema_version") != 1:
            raise ValueError("unsupported baseline schema")
    except (OSError, ValueError, json.JSONDecodeError):
        memory_samples: list[float] = []
        io_samples: list[float] = []
        sample_count = max(1, int(args.baseline_seconds / max(1.0, args.interval)))
        for index in range(sample_count):
            memory_samples.append(_psi_full("/proc/pressure/memory"))
            io_samples.append(_psi_full("/proc/pressure/io"))
            if index + 1 < sample_count:
                time.sleep(max(1.0, args.interval))
        host_baseline = {
            "schema_version": 1,
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": args.baseline_seconds,
            "sample_count": sample_count,
            "memory_psi_full_avg10": {
                "median": statistics.median(memory_samples),
                "p95": _percentile(memory_samples, 0.95),
            },
            "io_psi_full_avg10": {
                "median": statistics.median(io_samples),
                "p95": _percentile(io_samples, 0.95),
            },
        }
        _write_json_atomic(baseline_path, host_baseline)

    initial_rows = _containers(args.project)
    event_baseline = _event_signature(initial_rows)
    child = subprocess.Popen(command, start_new_session=True)
    pressure_failures = 0
    reason: str | None = None
    previous_io: dict[str, int] = {
        row["name"]: int((row.get("io_stat") or {}).get("wbytes", 0))
        for row in initial_rows
    }
    previous_io_at: float | None = time.monotonic()
    history: list[tuple[float, int, int]] = []
    with report.open("a", encoding="utf-8") as handle:
        while child.poll() is None:
            try:
                mem = _meminfo()
                memory_psi = _psi_full("/proc/pressure/memory")
                io_psi = _psi_full("/proc/pressure/io")
                rows = _containers(args.project)
                signature = _event_signature(rows)
                now_mono = time.monotonic()
                current_io = {
                    row["name"]: int((row.get("io_stat") or {}).get("wbytes", 0))
                    for row in rows
                }
                write_rate = None
                if previous_io_at is not None and now_mono > previous_io_at:
                    write_rate = sum(
                        max(0, value - previous_io.get(name, 0))
                        for name, value in current_io.items()
                    ) / (now_mono - previous_io_at)
                previous_io = current_io
                previous_io_at = now_mono
                hard = []
                soft: list[str] = []
                corroborating: list[str] = []
                if mem["memory_available_bytes"] < args.memory_floor_mib * MIB:
                    soft.append("host_memory_below_reference")
                if mem["swap_free_ratio"] < args.swap_free_ratio:
                    soft.append("host_swap_below_reference")
                swap_pages = _swap_pages()
                history.append((now_mono, int(mem["memory_available_bytes"]), swap_pages))
                history = [item for item in history if now_mono - item[0] <= 60.0]
                if len(history) >= 2:
                    elapsed = max(1.0, history[-1][0] - history[0][0])
                    memory_drop = history[0][1] - history[-1][1]
                    swap_rate = max(0, history[-1][2] - history[0][2]) * PAGE_SIZE * 60.0 / elapsed
                    if memory_drop >= args.memory_drop_mib * MIB:
                        corroborating.append("memory_decline")
                    if swap_rate >= args.swap_activity_mib_per_minute * MIB:
                        corroborating.append("active_swapping")
                else:
                    memory_drop = 0
                    swap_rate = 0.0
                if write_rate is not None and write_rate >= args.application_write_mib_per_second * MIB:
                    corroborating.append("application_write_rate")
                probe = _load_probe_state(args.probe_state)
                if float(probe.get("foreground_p95_seconds") or 0) >= args.foreground_p95_seconds:
                    corroborating.append("foreground_latency")
                if int(probe.get("nas_probe_failures") or 0) >= 3:
                    corroborating.append("nas_probe_failures")
                memory_delta = memory_psi - float(
                    host_baseline["memory_psi_full_avg10"]["p95"]
                )
                io_delta = io_psi - float(host_baseline["io_psi_full_avg10"]["p95"])
                relative_pressure = (
                    memory_delta >= args.memory_psi_delta or io_delta >= args.io_psi_delta
                )
                if relative_pressure:
                    soft.append("psi_above_idle_baseline")
                if args.policy == "load-soak" and relative_pressure and corroborating:
                    pressure_failures += 1
                else:
                    pressure_failures = 0
                if pressure_failures >= args.consecutive:
                    hard.append("attributed_sustained_pressure")
                for name, current in signature.items():
                    if "pressure-memory" in name:
                        # This one disposable cgroup is explicitly allowed to
                        # touch its own finite max counter. Host pressure and
                        # every application container remain hard-gated.
                        continue
                    # Acceptance containers are often created after the
                    # guardian starts. Their event counters must be compared
                    # with zero; treating the first observed value as the
                    # baseline would hide max/OOM/restart events that happen
                    # during container startup.
                    old = event_baseline.get(name, (0, 0, 0))
                    if any(now > before for now, before in zip(current, old, strict=True)):
                        hard.append(f"container_event:{name}")
                for row in rows:
                    if row.get("oom_killed") and "pressure-memory" not in row["name"]:
                        hard.append(f"container_oom_killed:{row['name']}")
                sample = {
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                    "memory": mem,
                    "memory_psi_full_avg10": memory_psi,
                    "io_psi_full_avg10": io_psi,
                    "psi_delta": {"memory": memory_delta, "io": io_delta},
                    "baseline": host_baseline,
                    "gate_policy": args.policy,
                    "containers": rows,
                    "auto_gallery_write_bytes_per_second": write_rate,
                    "application_io_rate": {"write_bytes_per_second": write_rate},
                    "memory_drop_60s_bytes": memory_drop,
                    "swap_activity_bytes_per_minute": swap_rate,
                    "corroborating_reasons": corroborating,
                    "soft_reasons": soft,
                    "hard_reasons": hard,
                }
                handle.write(json.dumps(sample, separators=(",", ":")) + "\n")
                handle.flush()
                if hard:
                    reason = ",".join(dict.fromkeys(hard))
                    _terminate(child)
                    break
            except Exception as exc:  # fail closed on guardian failure
                reason = f"guardian_error:{type(exc).__name__}:{exc}"
                _terminate(child)
                break
            time.sleep(max(1.0, args.interval))
    if reason:
        print(f"acceptance guardian stopped the workload: {reason}", file=sys.stderr)
        return 2
    return int(child.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
