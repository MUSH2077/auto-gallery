#!/usr/bin/env python3
"""Validate the two-hour NAS coexistence report against hard acceptance SLOs."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB


def percentile(values: list[float], value: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * value) - 1))]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--expected-duration", type=float, default=7200)
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    if not (root / ".auto-gallery-test-root").is_file():
        parser.error("not a marked acceptance root")
    samples = load_jsonl(root / "reports/coexistence-guardian.jsonl")
    latencies = load_jsonl(root / "reports/latency.jsonl")
    health = load_jsonl(root / "reports/health.jsonl")
    if len(samples) < 2:
        raise SystemExit("coexistence report has too few samples")
    started = datetime.fromisoformat(samples[0]["sampled_at"])
    ended = datetime.fromisoformat(samples[-1]["sampled_at"])
    elapsed = (ended - started).total_seconds()
    failures: list[str] = []
    if elapsed < max(0.0, args.expected_duration - 15):
        failures.append(f"duration {elapsed:.1f}s < {args.expected_duration:.1f}s")

    available = [float(row["memory"]["memory_available_bytes"]) for row in samples]
    if percentile(available, 0.01) < 1.25 * GIB:
        failures.append("MemAvailable p01 is below 1.25GiB")
    swap_free = [float(row["memory"]["swap_free_bytes"]) for row in samples]
    if swap_free[0] - min(swap_free) > 256 * MIB:
        failures.append("swap growth exceeded 256MiB from the soak baseline")
    write_rates = [
        float(row["auto_gallery_write_bytes_per_second"])
        for row in samples
        if row.get("auto_gallery_write_bytes_per_second") is not None
    ]
    if not write_rates or percentile(write_rates, 0.95) >= 10 * MIB:
        failures.append("auto-gallery write bandwidth p95 is not below 10MiB/s")

    for probe in ("backend", "admin", "nas"):
        values = [float(row["seconds"]) for row in latencies if row.get("probe") == probe]
        if not values or percentile(values, 0.95) >= 0.5:
            failures.append(f"{probe} latency p95 is not below 500ms")

    peaks: dict[str, int] = {}
    for sample in samples:
        for container in sample.get("containers") or []:
            peak = container.get("memory_peak_bytes")
            if peak is not None:
                peaks[container["name"]] = max(peaks.get(container["name"], 0), int(peak))
    import_peaks = [value for name, value in peaks.items() if "worker-import" in name]
    if not import_peaks or max(import_peaks) >= 400 * MIB:
        failures.append("Import Worker memory.peak is not below 400MiB")

    search_waiting = [
        int((((row.get("business") or {}).get("outboxes") or {}).get("search") or {}).get("waiting") or 0)
        for row in health
    ]
    if not search_waiting or max(search_waiting) <= 0 or search_waiting[-1] >= max(search_waiting):
        failures.append("search outbox did not demonstrate positive progress")

    summary = {
        "result": "fail" if failures else "pass",
        "duration_seconds": elapsed,
        "memory_available_p01_bytes": percentile(available, 0.01),
        "swap_growth_bytes": swap_free[0] - min(swap_free),
        "write_p95_bytes_per_second": percentile(write_rates, 0.95) if write_rates else None,
        "latency_p95_seconds": {
            probe: percentile([float(row["seconds"]) for row in latencies if row.get("probe") == probe], 0.95)
            for probe in ("backend", "admin", "nas")
            if any(row.get("probe") == probe for row in latencies)
        },
        "import_memory_peak_bytes": max(import_peaks) if import_peaks else None,
        "search_outbox_waiting_max": max(search_waiting) if search_waiting else None,
        "search_outbox_waiting_final": search_waiting[-1] if search_waiting else None,
        "failures": failures,
    }
    (root / "reports/coexistence-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
