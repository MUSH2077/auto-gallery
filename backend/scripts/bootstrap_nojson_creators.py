"""Generate minimal metadata JSONs for creators missing them, then import.

Usage:
    docker exec auto-gallery-backend-1 python3 scripts/bootstrap_nojson_creators.py --dry-run
    docker exec auto-gallery-backend-1 python3 scripts/bootstrap_nojson_creators.py

For creators in FileIndex with images but zero metadata JSONs, this:
1. Creates a minimal gallery-dl-compatible JSON per work directory
2. Registers files in storage_artifacts
3. Enqueues import jobs
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

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip"}


def generate_minimal_json(work_id: str, creator_dir: str) -> dict:
    return {
        "id": int(work_id) if work_id.isdigit() else work_id,
        "title": "",
        "type": "illust",
        "caption": "",
        "restrict": 0,
        "user": {"id": 0, "name": creator_dir, "account": creator_dir},
        "tags": [],
        "date": None,
        "_bootstrap_note": "Minimal metadata — re-sync with subscription for full data",
    }


async def bootstrap_nojson(dry_run: bool = False):
    download_root = Path(settings.download_root)
    fi_path = download_root / ".file-index.sqlite3"

    conn = sqlite3.connect(str(fi_path))
    rows = conn.execute("""
        SELECT source, creator_dir, COUNT(*) as cnt
        FROM file_index
        GROUP BY source, creator_dir
        HAVING SUM(CASE WHEN file_type='metadata_json' THEN 1 ELSE 0 END) = 0
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No creators without JSONs found.")
        return

    total_creators = len(rows)
    total_files = sum(r[2] for r in rows)
    print(f"Found {total_files} images across {total_creators} creators without JSONs")

    if dry_run:
        print("DRY RUN — first 10 creators:")
        for source, creator_dir, cnt in rows[:10]:
            print(f"  {source}/{creator_dir}: {cnt} images")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        return

    enqueued = 0
    skipped = 0
    errors = 0

    for idx, (source, creator_dir, file_count) in enumerate(rows):
        source_root = download_root / source / creator_dir
        if not source_root.exists():
            skipped += 1
            continue

        try:
            work_dirs = sorted(
                d for d in source_root.iterdir()
                if d.is_dir() and any(
                    f.is_file() and f.suffix.lower() in IMG_EXTS
                    for f in d.iterdir()
                )
            )
            if not work_dirs:
                skipped += 1
                continue

            # Generate minimal JSON per work
            for wd in work_dirs:
                jf = wd / f"{wd.name}_p0.json"
                if not jf.exists():
                    jf.write_text(
                        json.dumps(generate_minimal_json(wd.name, creator_dir), ensure_ascii=False),
                        encoding="utf-8",
                    )

            work_count = len(work_dirs)

            async with async_session() as db:
                # Try matching by display_name first
                sub_id = None
                ss_id = None
                source_url = f"bootstrap:{source}/{creator_dir}"

                sc_result = await db.execute(
                    select(SourceCreator).where(
                        SourceCreator.source == source,
                        SourceCreator.display_name == creator_dir,
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
                        ss_result = await db.execute(
                            select(SubscriptionSource).where(
                                SubscriptionSource.subscription_id == sub.id,
                                SubscriptionSource.source == source,
                            )
                        )
                        ss = ss_result.scalar_one_or_none()
                        if ss:
                            ss_id = ss.id
                            source_url = ss.source_url

                # Fallback: shared bootstrap subscription
                if not sub_id:
                    from app.models.creator import Creator
                    br = await db.execute(
                        select(Creator).where(Creator.name == "_bootstrap_recovery")
                    )
                    bc = br.scalar_one_or_none()
                    if bc:
                        sr = await db.execute(
                            select(Subscription).where(Subscription.creator_id == bc.id)
                        )
                        bs = sr.scalar_one_or_none()
                        if bs:
                            sub_id = bs.id
                            srr = await db.execute(
                                select(SubscriptionSource).where(
                                    SubscriptionSource.subscription_id == bs.id,
                                    SubscriptionSource.source == source,
                                )
                            )
                            bss = srr.scalar_one_or_none()
                            if bss:
                                ss_id = bss.id
                                source_url = bss.source_url

                dj_id = uuid4()
                db.add(DownloadJob(
                    id=dj_id, subscription_id=sub_id,
                    subscription_source_id=ss_id,
                    source=source, source_url=source_url,
                    status="complete",  # bypass active-source unique constraint
                    user_note=f"Bootstrap no-JSON: {work_count} works",
                ))

                rows_list = []
                seen = set()
                for wd in work_dirs:
                    for fp in sorted(wd.iterdir()):
                        if fp.is_file():
                            row = artifact_row(fp, download_root, dj_id)
                            if row and row["file_path"] not in seen:
                                seen.add(row["file_path"])
                                rows_list.append(row)

                if rows_list:
                    await ArtifactLedger(db).upsert_many(rows_list)

                ij_id = uuid4()
                import_job = ImportJob(
                    id=ij_id, download_job_id=dj_id,
                    status="enqueued",
                    user_note=f"Bootstrap no-JSON: {work_count} works",
                    progress_stage="enqueued",
                    progress_works_total=work_count,
                )
                db.add(import_job)
                await db.flush()
                prepared = await prepare_import_dispatch(
                    db,
                    import_job,
                    job_timeout=7200,
                    action="bootstrap-nojson",
                )
                await db.commit()
                publication = await publish_prepared_import(
                    db,
                    ij_id,
                    prepared.rq_job_id,
                )
                if publication == "invalid":
                    raise RuntimeError("Invalid bootstrap no-JSON import publication")
            enqueued += 1
            print(f"[{idx+1}/{total_creators}] {source}/{creator_dir}: {work_count} works → {ij_id}")

        except Exception as e:
            errors += 1
            print(f"[{idx+1}/{total_creators}] ERR {source}/{creator_dir}: {e}")

    print(f"\nDone. Enqueued: {enqueued}, Skipped: {skipped}, Errors: {errors}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(bootstrap_nojson(dry_run=dry))
