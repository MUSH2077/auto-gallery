"""Import gallery-dl files already on disk into the DB without re-downloading.

Scans DOWNLOAD_ROOT/{source}/, registers metadata + image artifacts that are not
yet imported (ledger state != 'done') under a synthetic 'recovery' download_job,
and enqueues the normal import pipeline. Idempotent: the ledger upsert mtime
guard keeps already-'done' files untouched and import-side claim_work dedups
works, so re-running never produces duplicates and never deletes/re-downloads."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.storage_artifact import StorageArtifact
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services.artifact_ledger import ArtifactLedger, artifact_row

logger = logging.getLogger(__name__)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip"}


async def _match_subscription_source(db: AsyncSession, source: str, source_creator_id: str):
    """Find the single-creator subscription_source whose URL identity matches."""
    try:
        provider = registry.get(source)
    except KeyError:
        return None
    ss_rows = (await db.execute(
        select(SubscriptionSource).where(SubscriptionSource.source == source)
    )).scalars().all()
    for ss in ss_rows:
        if ss.source_url and str(provider.get_creator_dir_from_url(ss.source_url)) == str(source_creator_id):
            return ss
    return None


async def reconcile_downloads_to_db(db: AsyncSession, options: dict, progress_callback=None) -> dict:
    """Import on-disk download files (not yet imported) into the DB. Idempotent."""
    from app.jobs.download import _enqueue_import

    root = Path(str(settings.download_root))
    source_filter = options.get("source")
    sources: list[str] = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            if source_filter and d.name != source_filter:
                continue
            try:
                registry.get(d.name)
            except KeyError:
                continue
            sources.append(d.name)

    stats = {"sources": 0, "creators": 0, "jobs": 0, "skipped_done": 0}
    for source in sources:
        stats["sources"] += 1
        provider = registry.get(source)
        scan_root = root / source

        done_paths = set((await db.execute(
            select(StorageArtifact.file_path).where(
                StorageArtifact.source == source, StorageArtifact.state == "done")
        )).scalars())

        groups: dict[str, list[Path]] = defaultdict(list)
        for jf in scan_root.rglob("*.json"):
            if not jf.is_file() or jf.parent == scan_root:
                continue
            rel = jf.relative_to(root)
            if len(rel.parts) < 3:
                continue
            if str(rel) in done_paths:
                stats["skipped_done"] += 1
                continue
            groups[rel.parts[1]].append(jf)

        total = len(groups)
        for i, (creator_dir, jsons) in enumerate(sorted(groups.items())):
            # Resolve identity from the first JSON to link the recovery job.
            ss = None
            try:
                with open(jsons[0]) as f:
                    first_raw = json.load(f)
                sc_id = provider.parse_source_creator(first_raw).get("source_creator_id")
                if sc_id:
                    ss = await _match_subscription_source(db, source, sc_id)
            except Exception:
                logger.warning("disk_import: could not read identity for %s/%s", source, creator_dir, exc_info=True)

            job = DownloadJob(
                source=source,
                source_url=(ss.source_url if ss else f"recovery://{source}/{creator_dir}"),
                status="downloaded",
                subscription_id=(ss.subscription_id if ss else None),
                subscription_source_id=(ss.id if ss else None),
            )
            db.add(job)
            await db.flush()

            rows = []
            seen: set[str] = set()
            new_paths: set[str] = set()
            for jf in jsons:
                for af in jf.parent.iterdir():
                    if af.is_file() and af.suffix.lower() in IMG_EXTS:
                        ar = artifact_row(af, root, job.id)
                        if ar and ar["file_path"] not in seen:
                            seen.add(ar["file_path"]); rows.append(ar)
                jr = artifact_row(jf, root, job.id)
                if jr and jr["file_path"] not in seen:
                    seen.add(jr["file_path"]); rows.append(jr); new_paths.add(str(jf))
            await ArtifactLedger(db).upsert_many(rows)
            await db.commit()

            await _enqueue_import(str(job.id), new_json_paths=new_paths)
            stats["creators"] += 1
            stats["jobs"] += 1
            if progress_callback:
                progress_callback({"phase": "running", "scanned": i + 1, "total": total, "source": source})

    return stats
