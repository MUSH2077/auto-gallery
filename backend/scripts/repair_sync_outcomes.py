"""Repair legacy successful no-op syncs that were persisted as errors.

Usage:
    python scripts/repair_sync_outcomes.py          # dry-run
    python scripts/repair_sync_outcomes.py --apply  # commit repairs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.database import async_session
from app.services.sync_outcome_repair import repair_legacy_sync_outcomes


async def main(*, apply: bool) -> None:
    async with async_session() as db:
        report = await repair_legacy_sync_outcomes(db, dry_run=not apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the repair; the default is a read-only dry-run.")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
