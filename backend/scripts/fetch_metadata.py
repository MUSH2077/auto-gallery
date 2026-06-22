"""Fetch complete metadata for creators missing JSONs via gallery-dl.

Usage:
    docker exec auto-gallery-backend-1 python3 scripts/fetch_metadata.py --dry-run
    docker exec auto-gallery-backend-1 python3 scripts/fetch_metadata.py
    docker exec auto-gallery-backend-1 python3 scripts/fetch_metadata.py pixiv ikuchan_kaoru

For each creator without metadata JSONs:
1. Find one work ID from directory listing
2. gallery-dl --range 1-1 on that artwork → discover Pixiv user ID
3. gallery-dl on user URL → write complete JSONs for all works (archive skips images)
4. Register artifacts and enqueue import
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from uuid import uuid4

from app.config import settings
from app.database import async_session
from app.models.creator import Creator
from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services.artifact_ledger import ArtifactLedger, artifact_row
from app.services.redis_client import get_redis
from rq import Queue
from sqlalchemy import select

GDL_TIMEOUT = 600


def find_work_ids(creator_dir: Path) -> list[str]:
    ids = set()
    for d in creator_dir.iterdir():
        if d.is_dir() and d.name.isdigit():
            ids.add(d.name)
        elif d.is_file() and d.suffix.lower() in {".jpg", ".png", ".gif", ".webp", ".bmp"}:
            stem = d.stem.split("_p")[0] if "_p" in d.stem else d.stem
            if stem.isdigit():
                ids.add(stem)
    return sorted(ids)


def run_gallerydl(url: str, dest: Path, range_spec: str = "") -> subprocess.CompletedProcess:
    cmd = ["gallery-dl", "--write-metadata", "--destination", str(dest)]
    if range_spec:
        cmd.extend(["--range", range_spec])
    cmd.append(url)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HTTPS_PROXY"] = "http://192.0.2.10:7890"
    env["HTTP_PROXY"] = "http://192.0.2.10:7890"
    return subprocess.run(cmd, capture_output=True, text=True, timeout=GDL_TIMEOUT, env=env)


def extract_user_from_json(json_dir: Path) -> int | None:
    for jf in sorted(json_dir.rglob("*.json")):
        try:
            uid = json.loads(jf.read_text(encoding="utf-8")).get("user", {}).get("id")
            if uid:
                return int(uid)
        except Exception:
            continue
    return None


async def fetch_metadata(source: str = "pixiv", creator_filter: str = None, dry_run: bool = False):
    download_root = Path(settings.download_root)
    fi_path = download_root / ".file-index.sqlite3"

    conn = sqlite3.connect(str(fi_path))
    query = """
        SELECT source, creator_dir, COUNT(*) as cnt
        FROM file_index
        GROUP BY source, creator_dir
        HAVING SUM(CASE WHEN file_type='metadata_json' THEN 1 ELSE 0 END) = 0
        ORDER BY cnt DESC
    """
    rows = conn.execute(query).fetchall()
    conn.close()

    if creator_filter:
        rows = [(s, c, n) for s, c, n in rows if c == creator_filter]

    if not rows:
        print("No creators without JSONs found.")
        return

    total = len(rows)
    total_files = sum(r[2] for r in rows)
    print(f"Found {total} creators ({total_files} images) without metadata JSONs")

    if dry_run:
        print("DRY RUN — first 10:")
        for s, c, n in rows[:10]:
            print(f"  {s}/{c}: {n} images")
        return

    ok = fail = 0
    for idx, (src, creator_dir, _) in enumerate(rows):
        creator_path = download_root / src / creator_dir
        work_ids = find_work_ids(creator_path)
        if not work_ids:
            print(f"[{idx+1}/{total}] SKIP {src}/{creator_dir}: no work IDs")
            fail += 1
            continue

        first_wid = work_ids[0]
        artwork_url = f"https://www.pixiv.net/artworks/{first_wid}"

        try:
            # Step 1: probe single artwork for user ID
            temp_dest = download_root / ".metadata_fetch" / creator_dir
            temp_dest.mkdir(parents=True, exist_ok=True)

            result = run_gallerydl(artwork_url, temp_dest, range_spec="1-1")
            if result.returncode != 0:
                tail = (result.stderr or "")[-300:]
                print(f"[{idx+1}/{total}] ERR {src}/{creator_dir}: probe failed\n  {tail}")
                fail += 1
                continue

            user_id = extract_user_from_json(temp_dest)
            if not user_id:
                print(f"[{idx+1}/{total}] ERR {src}/{creator_dir}: no user ID in probe JSON")
                fail += 1
                continue

            user_url = f"https://www.pixiv.net/users/{user_id}"
            print(f"[{idx+1}/{total}] {src}/{creator_dir}: user_id={user_id}")

            # Step 2: fetch metadata for all works (archive skips images)
            # gallery-dl may write to a different directory name (using Pixiv
            # account name vs our numeric user ID directory). After the run,
            # find all new JSONs and move them to the correct location.
            before_dirs = set(
                d for d in (download_root / src).iterdir() if d.is_dir()
            )

            result = run_gallerydl(user_url, download_root)
            if result.returncode != 0:
                tail = (result.stderr or "")[-300:]
                print(f"  ERR: user scan failed\n  {tail}")
                fail += 1
                continue

            # Find and move new JSONs to the correct creator directory
            after_dirs = set(
                d for d in (download_root / src).iterdir() if d.is_dir()
            )
            new_dirs = after_dirs - before_dirs
            new_jsons = []
            for nd in new_dirs:
                for jf in nd.rglob("*.json"):
                    # Move to matching work directory in our existing tree
                    work_id = jf.parent.name
                    target_dir = creator_path / work_id
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / jf.name
                    if not target.exists():
                        jf.rename(target)
                        new_jsons.append(target)
                # Clean up the gallery-dl-created directory
                import shutil
                try:
                    shutil.rmtree(nd)
                except Exception:
                    pass

            # Also check if gallery-dl wrote directly to the existing dir
            for jf in sorted(creator_path.rglob("*.json")):
                if jf not in new_jsons:
                    new_jsons.append(jf)

            print(f"  OK: {len(new_jsons)} JSONs")

            # Step 3: register + enqueue import
            async with async_session() as db:
                sub_id = None
                ss_id = None
                source_url = user_url

                sc = (await db.execute(
                    select(SourceCreator).where(
                        SourceCreator.source == src,
                        SourceCreator.source_creator_id == str(user_id),
                    )
                )).scalar_one_or_none()
                if sc and sc.creator_id:
                    sub = (await db.execute(
                        select(Subscription).where(Subscription.creator_id == sc.creator_id)
                    )).scalar_one_or_none()
                    if sub:
                        sub_id = sub.id
                        ss = (await db.execute(
                            select(SubscriptionSource).where(
                                SubscriptionSource.subscription_id == sub.id,
                                SubscriptionSource.source == src,
                            )
                        )).scalar_one_or_none()
                        if ss:
                            ss_id = ss.id
                            source_url = ss.source_url

                if not sub_id:
                    br = await db.execute(select(Creator).where(Creator.name == "_bootstrap_recovery"))
                    bc = br.scalar_one_or_none()
                    if bc:
                        sr = await db.execute(select(Subscription).where(Subscription.creator_id == bc.id))
                        bs = sr.scalar_one_or_none()
                        if bs:
                            sub_id = bs.id
                            srr = await db.execute(
                                select(SubscriptionSource).where(
                                    SubscriptionSource.subscription_id == bs.id,
                                    SubscriptionSource.source == src,
                                )
                            )
                            bss = srr.scalar_one_or_none()
                            if bss:
                                ss_id = bss.id

                dj_id = uuid4()
                db.add(DownloadJob(
                    id=dj_id, subscription_id=sub_id, subscription_source_id=ss_id,
                    source=src, source_url=source_url, status="complete",
                    user_note=f"Metadata fetch: {len(new_jsons)} works",
                ))

                rows_list = []
                seen = set()
                for jf in new_jsons:
                    row = artifact_row(jf, download_root, dj_id)
                    if row and row["file_path"] not in seen:
                        seen.add(row["file_path"])
                        rows_list.append(row)
                    for img in sorted(jf.parent.iterdir()):
                        if img.is_file() and img.suffix.lower() in {
                            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip",
                        }:
                            ir = artifact_row(img, download_root, dj_id)
                            if ir and ir["file_path"] not in seen:
                                seen.add(ir["file_path"])
                                rows_list.append(ir)

                if rows_list:
                    await ArtifactLedger(db).upsert_many(rows_list)

                ij_id = uuid4()
                db.add(ImportJob(
                    id=ij_id, download_job_id=dj_id,
                    status="enqueued",
                    user_note=f"Metadata fetch: {len(new_jsons)} works",
                    progress_stage="enqueued",
                    progress_works_total=len(new_jsons),
                ))
                await db.commit()

            Queue(name="imports", connection=get_redis()).enqueue(
                "app.jobs.import_runner.run_import_job",
                str(ij_id), job_timeout=7200,
            )
            ok += 1

        except subprocess.TimeoutExpired:
            print(f"[{idx+1}/{total}] ERR {src}/{creator_dir}: timeout")
            fail += 1
        except Exception as e:
            print(f"[{idx+1}/{total}] ERR {src}/{creator_dir}: {e}")
            fail += 1

    print(f"\nDone. OK: {ok}, Failed: {fail}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    cfilter = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--dry-run":
            dry = True
        elif not a.startswith("--"):
            if i == 1:
                cfilter = a
    asyncio.run(fetch_metadata(dry_run=dry, creator_filter=cfilter))
