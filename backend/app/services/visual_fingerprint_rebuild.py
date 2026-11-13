"""Resumable visual-fingerprint upgrade through the ordinary media outbox.

Run with pipeline consumers stopped. This command never deletes evidence/groups.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import func, or_, select, update

from app.database import async_session, engine
from app.models import Asset, MediaDerivativeOutbox
from app.services.media_derivatives import (
    MEDIA_ALGORITHM_VERSION, PHASH_ALGORITHM_VERSION, _safe_source_path, _now,
    process_media_derivative_outbox, request_media_derivatives,
)


async def legacy_assets(batch_size):
    cursor = None
    while True:
        async with async_session() as db:
            query = select(Asset).where(
                Asset.phash.is_not(None),
                Asset.phash_version.is_distinct_from(PHASH_ALGORITHM_VERSION),
            ).order_by(Asset.id).limit(batch_size)
            if cursor is not None:
                query = query.where(Asset.id > cursor)
            rows = list((await db.execute(query)).scalars())
        if not rows:
            return
        cursor = rows[-1].id
        yield rows


async def enqueue_upgrade(batch_size=100):
    counts = {"queued": 0, "unavailable": 0}
    # Old pending intents must not execute new sampling with an old media label.
    # Consumers are stopped for this explicit upgrade. Reset processing claims
    # too, so an interrupted CLI can resume before the 30-minute lease expires.
    # Invalidating the lease fences a worker that was still finishing.
    async with async_session() as db:
        await db.execute(update(MediaDerivativeOutbox).where(
            or_(MediaDerivativeOutbox.algorithm_version != MEDIA_ALGORITHM_VERSION,
                MediaDerivativeOutbox.state == "processing"),
            MediaDerivativeOutbox.state.in_(("pending", "failed", "processing")),
            MediaDerivativeOutbox.requested["phash"].as_boolean().is_(True),
        ).values(algorithm_version=MEDIA_ALGORITHM_VERSION, state="pending",
                 available_at=_now(), lease_expires_at=None, attempts=0, last_error=None))
        await db.commit()
    async for assets in legacy_assets(batch_size):
        requests = []
        for asset in assets:
            path = _safe_source_path(asset.file_path)
            try:
                stat = path.stat()
            except FileNotFoundError:
                # Purged/quarantined historical members retain their old hash.
                # Only currently available original files can be recomputed.
                counts["unavailable"] += 1
                continue
            requests.append({
                "asset_id": asset.id,
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "requested": {"sha256": True, "phash": True},
            })
        async with async_session() as db:
            counts["queued"] += await request_media_derivatives(db, requests)
            await db.commit()
    return counts


async def upgrade_status(batch_size=100):
    counts = {"remaining_available": 0, "unavailable_historical": 0}
    async for assets in legacy_assets(batch_size):
        for asset in assets:
            key = "remaining_available" if _safe_source_path(asset.file_path).is_file() else "unavailable_historical"
            counts[key] += 1
    async with async_session() as db:
        counts["pending_fingerprints"] = int((await db.execute(
            select(func.count()).select_from(MediaDerivativeOutbox).where(
                MediaDerivativeOutbox.state.in_(("pending", "failed", "processing")),
                MediaDerivativeOutbox.requested["phash"].as_boolean().is_(True),
            )
        )).scalar_one())
    return counts


async def run(args):
    result = {"target_version": PHASH_ALGORITHM_VERSION}
    try:
        if args.enqueue:
            result["enqueue"] = await enqueue_upgrade(args.batch_size)
        if args.process:
            totals = {"processed": 0, "failed": 0}
            while True:
                cooldown = {}
                progress = await process_media_derivative_outbox(limit=1, cooldown_result=cooldown)
                for key in totals:
                    totals[key] += int(progress[key])
                if progress["failed"] or progress.get("resource_deferred") or not progress["claimed"]:
                    break
                if cooldown.get("seconds"):
                    await asyncio.sleep(cooldown["seconds"])
            result["process"] = totals
        result["status"] = await upgrade_status(args.batch_size)
        print(json.dumps(result))
        return int(bool(result["status"]["remaining_available"] or result["status"]["pending_fingerprints"]
                        or result.get("process", {}).get("failed")))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enqueue", action="store_true")
    parser.add_argument("--process", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("batch-size must be between 1 and 500")
    raise SystemExit(asyncio.run(run(args)))
