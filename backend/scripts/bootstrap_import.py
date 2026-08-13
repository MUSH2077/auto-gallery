"""Batch bootstrap import for all stuck files — one-off recovery script.

Usage:
    docker exec auto-gallery-backend-1 python3 scripts/bootstrap_import.py
    docker exec auto-gallery-backend-1 python3 scripts/bootstrap_import.py --dry-run

Reads FileIndex SQLite for all import_status='new' entries, groups by
(source, creator_dir), reads metadata JSON to extract the real user ID,
matches against source_creators → subscriptions, then registers files
in storage_artifacts and enqueues import jobs.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from uuid import uuid4

from app.config import settings
from app.database import async_session
from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services.artifact_ledger import ArtifactLedger, artifact_row
from app.services.import_dispatch import prepare_import_dispatch, publish_prepared_import
from sqlalchemy import select


def _extract_user_id(json_path: Path) -> str | None:
    """Read a gallery-dl metadata JSON and extract the numeric user/creator ID."""
    try:
        with open(json_path) as f:
            data = json.load(f)
        user = data.get("user") or {}
        uid = user.get("id")
        return str(uid) if uid is not None else None
    except Exception:
        return None


async def bootstrap_all(dry_run: bool = False):
    download_root = Path(settings.download_root)
    fi_path = download_root / ".file-index.sqlite3"

    if not fi_path.exists():
        print("FileIndex not found — nothing to migrate.")
        return

    conn = sqlite3.connect(str(fi_path))
    rows = conn.execute(
        "SELECT source, creator_dir, COUNT(*) as cnt "
        "FROM file_index WHERE import_status='new' AND file_type='metadata_json' "
        "GROUP BY source, creator_dir ORDER BY cnt DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No stuck files found.")
        return

    total_creators = len(rows)
    total_files = sum(r[2] for r in rows)
    print(f"Found {total_files} stuck metadata files across {total_creators} creators")

    if dry_run:
        print("DRY RUN — listing affected creators:")
        for source, creator_dir, cnt in rows:
            print(f"  {source}/{creator_dir}: {cnt} files")
        return

    enqueued = 0
    skipped = 0
    errors = 0

    for idx, (source, creator_dir, file_count) in enumerate(rows):
        source_root = download_root / source / creator_dir
        if not source_root.exists():
            print(f"[{idx+1}/{total_creators}] SKIP {source}/{creator_dir}: dir not found")
            skipped += 1
            continue

        try:
            # Find metadata JSONs first
            json_files = sorted(source_root.rglob("*_p0.json"))
            if not json_files:
                json_files = sorted(source_root.rglob("*.json"))
            if not json_files:
                print(f"[{idx+1}/{total_creators}] SKIP {source}/{creator_dir}: no JSONs")
                skipped += 1
                continue

            # Extract real user ID from first JSON
            user_id = _extract_user_id(json_files[0])

            async with async_session() as db:
                sub_id = None
                source_url = f"bootstrap:{source}/{creator_dir}"
                ss_id = None

                # Strategy 1: match by numeric user ID from JSON
                if user_id:
                    sc_result = await db.execute(
                        select(SourceCreator).where(
                            SourceCreator.source == source,
                            SourceCreator.source_creator_id == user_id,
                        )
                    )
                    sc = sc_result.scalar_one_or_none()
                    if sc and sc.creator_id:
                        sub_result = await db.execute(
                            select(Subscription).where(Subscription.creator_id == sc.creator_id)
                        )
                        sub = sub_result.scalar_one_or_none()
                        if sub:
                            sub_id = sub.id

                # Strategy 2: match by source URL containing creator_dir
                if not sub_id:
                    ss_result = await db.execute(
                        select(SubscriptionSource).where(
                            SubscriptionSource.source == source,
                            SubscriptionSource.source_url.contains(creator_dir),
                        )
                    )
                    ss = ss_result.scalar_one_or_none()
                    if ss:
                        sub_id = ss.subscription_id
                        ss_id = ss.id
                        source_url = ss.source_url

                # Strategy 3: match source_creator by directory name
                if not sub_id:
                    sc_result = await db.execute(
                        select(SourceCreator).where(
                            SourceCreator.source == source,
                            SourceCreator.source_creator_id == creator_dir,
                        )
                    )
                    sc = sc_result.scalar_one_or_none()
                    if sc and sc.creator_id:
                        sub_result = await db.execute(
                            select(Subscription).where(Subscription.creator_id == sc.creator_id)
                        )
                        sub = sub_result.scalar_one_or_none()
                        if sub:
                            sub_id = sub.id

                if not sub_id:
                    print(f"[{idx+1}/{total_creators}] SKIP {source}/{creator_dir}: no subscription (uid={user_id})")
                    skipped += 1
                    continue

                # Get subscription_source for the matched subscription
                if not ss_id:
                    ss_row = await db.execute(
                        select(SubscriptionSource).where(
                            SubscriptionSource.subscription_id == sub_id,
                            SubscriptionSource.source == source,
                        )
                    )
                    ss_match = ss_row.scalar_one_or_none()
                    if ss_match:
                        ss_id = ss_match.id
                        source_url = ss_match.source_url

                # Create download job
                dj_id = uuid4()
                db.add(DownloadJob(
                    id=dj_id,
                    subscription_id=sub_id,
                    subscription_source_id=ss_id,
                    source=source,
                    source_url=source_url,
                    status="downloaded",
                    user_note=f"Bootstrap recovery: {len(json_files)} works",
                ))

                # Register artifacts
                rows_list = []
                seen = set()
                for jf in json_files:
                    row = artifact_row(jf, download_root, dj_id)
                    if row and row["file_path"] not in seen:
                        seen.add(row["file_path"])
                        rows_list.append(row)
                    for img in sorted(jf.parent.iterdir()):
                        if img.is_file() and img.suffix.lower() in {
                            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip",
                        }:
                            img_row = artifact_row(img, download_root, dj_id)
                            if img_row and img_row["file_path"] not in seen:
                                seen.add(img_row["file_path"])
                                rows_list.append(img_row)

                if rows_list:
                    await ArtifactLedger(db).upsert_many(rows_list)

                ij_id = uuid4()
                import_job = ImportJob(
                    id=ij_id,
                    download_job_id=dj_id,
                    status="enqueued",
                    user_note=f"Bootstrap recovery: {len(json_files)} works",
                    progress_stage="enqueued",
                    progress_works_total=len(json_files),
                )
                db.add(import_job)
                await db.flush()
                prepared = await prepare_import_dispatch(
                    db,
                    import_job,
                    job_timeout=7200,
                    action="bootstrap-import",
                )
                await db.commit()
                publication = await publish_prepared_import(
                    db,
                    ij_id,
                    prepared.rq_job_id,
                )
                if publication == "invalid":
                    raise RuntimeError("Invalid bootstrap import publication")
            enqueued += 1
            print(f"[{idx+1}/{total_creators}] OK {source}/{creator_dir}: {len(json_files)} works → {ij_id}")

        except Exception as e:
            err_str = str(e)
            if "uq_download_jobs_active_source" in err_str:
                print(f"[{idx+1}/{total_creators}] SKIP {source}/{creator_dir}: active download job already exists")
                skipped += 1
            else:
                errors += 1
                print(f"[{idx+1}/{total_creators}] ERR {source}/{creator_dir}: {e}")

    print(f"\nDone. Enqueued: {enqueued}, Skipped: {skipped}, Errors: {errors}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(bootstrap_all(dry_run=dry))
