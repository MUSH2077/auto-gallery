#!/usr/bin/env python3
"""Backfill stored video metadata and derived posters.

Defaults to a read-only report. Pass --apply to persist metadata and write
derived WebP files under LIBRARY_ROOT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import async_session  # noqa: E402
from app.services.video_backfill import backfill_video_assets  # noqa: E402


async def _run(apply: bool, force: bool) -> None:
    async with async_session() as db:
        report = await backfill_video_assets(db, apply=apply, force=force)
    print(json.dumps(report, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="persist metadata and derived posters")
    parser.add_argument("--force", action="store_true", help="regenerate existing posters")
    args = parser.parse_args()
    asyncio.run(_run(args.apply, args.force))


if __name__ == "__main__":
    main()
