#!/usr/bin/env python3
"""Bounded anonymous-memory pressure for the disposable acceptance cgroup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

MIB = 1024 * 1024
MAX_TARGET_MIB = 384
STEP_MIB = 16


def _cgroup_limit() -> int | None:
    for path in (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value != "max":
                return int(value)
        except (OSError, ValueError):
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-mib", type=int, default=MAX_TARGET_MIB)
    parser.add_argument("--step-mib", type=int, default=STEP_MIB)
    parser.add_argument("--step-seconds", type=float, default=1.0)
    parser.add_argument("--hold-seconds", type=float, default=30.0)
    parser.add_argument("--require-container", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.target_mib <= MAX_TARGET_MIB:
        parser.error(f"--target-mib must be between 1 and {MAX_TARGET_MIB}")
    if not 1 <= args.step_mib <= STEP_MIB:
        parser.error(f"--step-mib must be between 1 and {STEP_MIB}")
    limit = _cgroup_limit()
    if args.require_container and limit is None:
        parser.error("a finite cgroup memory limit is required")
    if limit is not None and args.target_mib * MIB > limit - 64 * MIB:
        parser.error("target must leave at least 64MiB below the cgroup limit")

    blocks: list[bytearray] = []
    allocated = 0
    while allocated < args.target_mib:
        chunk = min(args.step_mib, args.target_mib - allocated)
        block = bytearray(chunk * MIB)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        blocks.append(block)
        allocated += chunk
        print(json.dumps({"event": "allocated", "mib": allocated}), flush=True)
        time.sleep(max(0.0, args.step_seconds))
    print(json.dumps({"event": "holding", "mib": allocated}), flush=True)
    time.sleep(max(0.0, args.hold_seconds))
    blocks.clear()
    print(json.dumps({"event": "released", "mib": allocated}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
