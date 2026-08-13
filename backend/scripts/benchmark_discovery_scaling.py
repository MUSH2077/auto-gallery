#!/usr/bin/env python3
"""Verify staging-delta discovery is insensitive to 100k existing files."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import time

from app.providers.x import XProvider
from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group

MAX_EXISTING_FILES = 100_000
MAX_CREATE_IOPS = 250


def _measure(metadata_paths: list[Path], allowed: set[Path], repeats: int) -> float:
    samples = []
    provider = XProvider()
    for _ in range(repeats):
        started = time.perf_counter()
        groups, invalid = group_metadata_by_work(provider, metadata_paths)
        if invalid or len(groups) != len(metadata_paths):
            raise RuntimeError(f"unexpected discovery result: groups={len(groups)} invalid={invalid}")
        for work_id, group in groups.items():
            files = media_files_for_group(group, work_id, allowed_paths=allowed)
            if len(files) != 1:
                raise RuntimeError(f"work {work_id} resolved {len(files)} media files")
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--works", type=int, default=200)
    parser.add_argument("--existing-files", type=int, default=MAX_EXISTING_FILES)
    parser.add_argument("--create-iops", type=int, default=MAX_CREATE_IOPS)
    parser.add_argument("--max-growth", type=float, default=1.20)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not 1 <= args.works <= 200:
        parser.error("--works must be in [1,200]")
    if not 0 <= args.existing_files <= MAX_EXISTING_FILES:
        parser.error(f"--existing-files must be in [0,{MAX_EXISTING_FILES}]")
    if not 1 <= args.create_iops <= MAX_CREATE_IOPS:
        parser.error(f"--create-iops must be in [1,{MAX_CREATE_IOPS}]")

    run_root = Path(args.root).resolve(strict=True)
    if not (run_root.parent / ".auto-gallery-test-root").is_file():
        parser.error("root must be directly below a marked acceptance run")
    work_dir = run_root / "flat"
    work_dir.mkdir(parents=True, exist_ok=True)
    metadata_paths: list[Path] = []
    allowed: set[Path] = set()
    for number in range(args.works):
        stem = f"delta-{number:06d}"
        metadata = work_dir / f"{stem}.json"
        media = work_dir / f"{stem}.jpg"
        metadata.write_text(
            json.dumps(
                {
                    "tweet_id": number + 1,
                    "filename": stem,
                    "num": 1,
                    "extension": "jpg",
                    "user": {"id": 1, "name": "acceptance"},
                }
            ),
            encoding="utf-8",
        )
        media.write_bytes(b"fixture")
        metadata_paths.append(metadata)
        allowed.update((metadata, media))

    baseline = _measure(metadata_paths, allowed, args.repeats)
    creation_started = time.monotonic()
    for number in range(args.existing_files):
        (work_dir / f"existing-{number:06d}.bin").touch(exist_ok=True)
        completed = number + 1
        due = completed / args.create_iops
        delay = due - (time.monotonic() - creation_started)
        if delay > 0:
            time.sleep(delay)
    after = _measure(metadata_paths, allowed, args.repeats)
    growth = after / max(baseline, 1e-9)
    payload = {
        "works": args.works,
        "existing_files": args.existing_files,
        "baseline_seconds": baseline,
        "after_seconds": after,
        "growth_ratio": growth,
        "max_growth_ratio": args.max_growth,
        "source_root_scans": 0,
        "result": "pass" if growth <= args.max_growth else "fail",
    }
    if args.output:
        output = Path(args.output)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0 if math.isfinite(growth) and growth <= args.max_growth else 1


if __name__ == "__main__":
    raise SystemExit(main())
