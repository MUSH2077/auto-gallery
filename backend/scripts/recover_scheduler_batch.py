"""Reconcile the recorded legacy sync batch. Defaults to read-only dry-run.

Usage: python -m scripts.recover_scheduler_batch [--apply]
"""

import argparse
import asyncio
import json

from app.database import async_session, engine
from app.services.scheduler_batches import recover_legacy_batch


async def main(apply=False):
    try:
        async with async_session() as db:
            result = await recover_legacy_batch(db, apply=apply)
            print(json.dumps(result, indent=2, default=str))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist an idempotent linked recovery batch")
    asyncio.run(main(parser.parse_args().apply))
