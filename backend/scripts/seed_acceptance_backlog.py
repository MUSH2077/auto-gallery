#!/usr/bin/env python3
"""Create bounded, idempotent search work for the isolated soak stack."""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session
from app.models import Work
from app.services.search_projection_outbox import request_search_projection


async def run(limit: int) -> int:
    async with async_session() as db:
        identities = list(
            (await db.execute(select(Work.id).order_by(Work.id).limit(limit))).scalars()
        )
        requested = await request_search_projection(db, identities)
        await db.commit()
    print(f"seeded_search_projection={requested}")
    return 0 if requested else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(max(1, min(args.limit, 10_000)))))
