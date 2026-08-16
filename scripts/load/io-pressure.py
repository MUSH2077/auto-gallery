#!/usr/bin/env python3
"""Rate- and path-bounded I/O pressure for an acceptance scratch directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import time

MIB = 1024 * 1024
MAX_RATE_MIB = 8.0
MAX_IOPS = 250
MAX_SIZE_MIB = 1024
MARKER = ".auto-gallery-test-root"


def _validated_root(raw: str) -> Path:
    root = Path(raw).resolve(strict=True)
    if not (root / MARKER).is_file():
        raise ValueError(f"scratch root must contain {MARKER}: {root}")
    scratch = (root / "io-scratch").resolve()
    if root not in scratch.parents:
        raise ValueError("scratch path escaped the marked test root")
    scratch.mkdir(mode=0o700, exist_ok=True)
    return scratch


def _pace(started: float, completed_bytes: int, rate: float) -> None:
    if rate <= 0:
        return
    due = completed_bytes / (rate * MIB)
    delay = due - (time.monotonic() - started)
    if delay > 0:
        time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--mode", choices=("sequential", "random", "fsync", "promotion"), default="sequential")
    parser.add_argument("--rate-mib", type=float, default=8.0)
    parser.add_argument("--iops", type=int, default=100)
    parser.add_argument("--size-mib", type=int, default=256)
    parser.add_argument("--duration", type=float, default=60.0)
    args = parser.parse_args()
    if not 0 < args.rate_mib <= MAX_RATE_MIB:
        parser.error(f"--rate-mib must be in (0,{MAX_RATE_MIB}]")
    if not 1 <= args.iops <= MAX_IOPS:
        parser.error(f"--iops must be in [1,{MAX_IOPS}]")
    if not 1 <= args.size_mib <= MAX_SIZE_MIB:
        parser.error(f"--size-mib must be in [1,{MAX_SIZE_MIB}]")
    try:
        scratch = _validated_root(args.root)
    except ValueError as exc:
        parser.error(str(exc))

    path = scratch / "pressure.bin"
    chunk = b"\0" * MIB
    started = time.monotonic()
    written = 0
    with path.open("w+b", buffering=0) as handle:
        if args.mode == "random":
            handle.truncate(args.size_mib * MIB)
            block = b"\x5a" * 4096
            operations = 0
            while time.monotonic() - started < args.duration:
                handle.seek(random.randrange(0, args.size_mib * 256) * 4096)
                handle.write(block)
                operations += 1
                delay = operations / args.iops - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(delay)
            written = operations * len(block)
        else:
            target = args.size_mib * MIB
            while written < target and time.monotonic() - started < args.duration:
                amount = min(len(chunk), target - written)
                handle.write(chunk[:amount])
                written += amount
                if args.mode == "fsync" and written % (64 * MIB) == 0:
                    os.fdatasync(handle.fileno())
                _pace(started, written, args.rate_mib)
            os.fdatasync(handle.fileno())
    if args.mode == "promotion":
        promoted = scratch / "promoted.bin"
        os.link(path, promoted)
        dir_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        promoted.unlink()
    elapsed = max(0.001, time.monotonic() - started)
    print(json.dumps({"event": "complete", "mode": args.mode, "bytes": written, "seconds": elapsed, "mib_per_second": written / MIB / elapsed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
