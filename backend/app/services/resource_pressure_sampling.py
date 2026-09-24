"""Host and cgroup pressure sampling without Redis or controller state."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    foreground_sample_generation: int = 0
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
        hostname = socket.gethostname()
        cgroup_id = f"{hostname}:{root}"
        try:
            membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
            membership_id = next(
                (
                    line.split("::", 1)[1].strip()
                    for line in membership.splitlines()
                    if "::" in line
                ),
                str(root),
            )
            cgroup_id = f"{hostname}:{membership_id}"
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
_foreground_latency_generation = 0
FOREGROUND_LATENCY_WINDOW_SECONDS = 5 * 60
LOCAL_CGROUP_WARNING_SECONDS = 60.0


def record_foreground_latency(path: str, duration_ms: float) -> None:
    """Middleware hook for work-list/search GET latency feedback."""

    normalized = str(path or "")
    if not (
        normalized.startswith("/api/v1/search")
        or (
            normalized.startswith("/api/v1/works")
            and normalized != "/api/v1/works/derivative-progress"
        )
    ):
        return
    try:
        value = max(0.0, float(duration_ms))
    except (TypeError, ValueError):
        return
    now = time.monotonic()
    with _foreground_latency_lock:
        global _foreground_latency_generation
        _foreground_latency_generation += 1
        _foreground_latencies.append((now, value))


def foreground_latency_snapshot(now: float | None = None) -> dict[str, Any]:
    now = time.monotonic() if now is None else now
    cutoff = now - FOREGROUND_LATENCY_WINDOW_SECONDS
    with _foreground_latency_lock:
        while _foreground_latencies and _foreground_latencies[0][0] < cutoff:
            _foreground_latencies.popleft()
        values = sorted(value for _, value in _foreground_latencies)
        generation = _foreground_latency_generation
    if not values:
        return {
            "p95_ms": None,
            "sample_count": 0,
            "sample_generation": generation,
            "window_seconds": 300,
        }
    index = min(len(values) - 1, max(0, int(0.95 * (len(values) - 1))))
    return {
        "p95_ms": round(values[index], 3),
        "sample_count": len(values),
        "sample_generation": generation,
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
        foreground_sample_generation=foreground["sample_generation"],
        sampled_at=datetime.now(timezone.utc).isoformat(),
    )


