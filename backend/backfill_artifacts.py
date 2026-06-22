"""One-time, resumable migration from the legacy SQLite file index."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

from app.config import settings
from app.database import async_session
from app.services.artifact_ledger import ArtifactLedger


async def backfill(batch_size: int = 2000) -> int:
    sqlite_path = Path(settings.download_root) / ".file-index.sqlite3"
    if not sqlite_path.exists():
        print("Legacy file index does not exist; nothing to migrate")
        return 0
    migrated = 0
    last_id = 0
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        while True:
            rows = connection.execute(
                "SELECT * FROM file_index WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            payload = []
            for row in rows:
                download_job_id = row["download_job_id"] or None
                try:
                    download_job_id = UUID(download_job_id) if download_job_id else None
                except ValueError:
                    download_job_id = None
                payload.append({
                    "id": uuid4(), "storage_root": row["storage_root"],
                    "file_path": row["file_path"], "source": row["source"],
                    "creator_dir": row["creator_dir"], "source_work_id": row["work_id"],
                    "file_name": row["file_name"], "artifact_type": row["file_type"],
                    "file_size": row["file_size"], "download_job_id": download_job_id,
                    "state": "done" if row["import_status"] == "done" else "new",
                })
            async with async_session() as db:
                migrated += await ArtifactLedger(db).upsert_many(payload, batch_size)
                await db.commit()
            last_id = rows[-1]["id"]
            print(f"Migrated {migrated} artifacts")
    finally:
        connection.close()
    return migrated


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=2000)
    args = parser.parse_args()
    asyncio.run(backfill(args.batch_size))
