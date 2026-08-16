"""Export, dry-run, or apply the frozen legacy subscription schedule migration."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from app.database import async_session
from app.services.subscription_schedule_repair import (
    apply_subscription_schedule_migration_entry,
    build_subscription_schedule_migration_plan,
)


def _write_snapshot(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = list(entries[0]) if entries else ["subscription_id"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(entries)


async def run(args: argparse.Namespace) -> dict:
    snapshot_path = Path(args.snapshot)
    if args.apply:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        entries = payload.get("entries") or []
        results = []
        async with async_session() as db:
            for entry in entries:
                result = await apply_subscription_schedule_migration_entry(db, entry)
                if result["status"] == "applied":
                    await db.commit()
                else:
                    await db.rollback()
                results.append(result)
        return {
            "snapshot": str(snapshot_path),
            "total": len(results),
            "counts": {
                status: sum(1 for item in results if item["status"] == status)
                for status in sorted({item["status"] for item in results})
            },
            "results": results,
        }

    ids = [UUID(value) for value in args.subscription_id]
    async with async_session() as db:
        entries = await build_subscription_schedule_migration_plan(
            db,
            subscription_ids=ids or None,
        )
        await db.rollback()
    _write_snapshot(snapshot_path, entries)
    return {
        "snapshot": str(snapshot_path),
        "csv": str(snapshot_path.with_suffix(".csv")),
        "total": len(entries),
        "providers": {
            provider: sum(1 for item in entries if item["selected_provider"] == provider)
            for provider in sorted({item["selected_provider"] for item in entries if item["selected_provider"]})
        },
        "unresolved": sum(1 for item in entries if not item["selected_source_id"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        default="/tmp/auto-gallery-subscription-schedule-migration.json",
    )
    parser.add_argument("--subscription-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
