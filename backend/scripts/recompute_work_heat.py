#!/usr/bin/env python3
"""Backfill source metrics and materialized work heat for existing libraries."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.database import async_session
from app.models import WorkSource
from app.services.work_heat import recompute_source_heat


async def run(requested_sources: tuple[str, ...] = ()) -> dict[str, object]:
    async with async_session() as db:
        sources = {
            source.strip().lower()
            for source in requested_sources
            if source.strip()
        }
        if not sources:
            rows = await db.execute(
                select(WorkSource.source).distinct().order_by(WorkSource.source)
            )
            sources = {str(source).strip().lower() for source in rows.scalars() if source}
        changed = await recompute_source_heat(db, sources)
        await db.commit()
        return {
            "status": "ok",
            "sources": sorted(sources),
            "changed_works": len(changed),
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill source engagement snapshots and work heat scores."
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Limit recomputation to one source; repeat for multiple sources",
    )
    args = parser.parse_args()
    result = asyncio.run(run(tuple(args.source)))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
