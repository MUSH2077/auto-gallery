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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.storage_artifact import StorageArtifact
from app.providers import registry
from app.services.artifact_ledger import ArtifactLedger, artifact_row
from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group
from app.services.disk_identity import extract_metadata_identity, provision_identity_for_disk_import
from app.services.settings import source_key_for_extractor

logger = logging.getLogger(__name__)

async def reconcile_downloads_to_db(db: AsyncSession, options: dict, progress_callback=None) -> dict:
    """Import on-disk download files (not yet imported) into the DB. Idempotent."""
    from app.jobs.download import _enqueue_import

    root = Path(str(settings.download_root))
    requested_source = options.get("source")
    source_filter = source_key_for_extractor(requested_source) if requested_source else None
    sources: list[tuple[str, str]] = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            canonical_source = source_key_for_extractor(d.name)
            if source_filter and canonical_source != source_filter:
                continue
            try:
                registry.get(canonical_source)
            except KeyError:
                continue
            sources.append((d.name, canonical_source))

    stats = {
        "sources": 0,
        "creators": 0,
        "jobs": 0,
        "skipped_done": 0,
        "skipped_invalid_metadata": 0,
        "danbooru_enriched": 0,
        "metadata_fallback": 0,
        "subscription_sources_created": 0,
        "import_job_ids": [],
    }
    parent_task_id = options.get("parent_task_id")
    # Recovery hammer: when the DB was cleared (e.g. creators deleted) but the
    # StorageArtifact ledger still marks files 'done', the normal 'done' guard
    # skips them and they never get re-imported. reset_ledger ignores the guard
    # and reprocesses everything (idempotent: import-side claim_work dedups).
    reset_ledger = bool(options.get("reset_ledger") or options.get("force"))
    stats["reset_ledger"] = reset_ledger
    for disk_source, source in sources:
        stats["sources"] += 1
        scan_root = root / disk_source

        if reset_ledger:
            done_paths = set()
        else:
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
            provisioned = None
            identity = None
            try:
                with open(jsons[0]) as f:
                    first_raw = json.load(f)
                identity = extract_metadata_identity(source, first_raw, creator_dir)
                provisioned = await provision_identity_for_disk_import(db, identity)
            except Exception:
                logger.warning("disk_import: could not provision identity for %s/%s", source, creator_dir, exc_info=True)
                await db.rollback()
                stats["skipped_invalid_metadata"] += 1
                if progress_callback:
                    progress_callback({"phase": "running", "scanned": i + 1, "total": total, "source": source})
                continue

            if provisioned.enrichment_status == "danbooru_found":
                stats["danbooru_enriched"] += 1
            else:
                stats["metadata_fallback"] += 1
            if provisioned.created_source:
                stats["subscription_sources_created"] += 1

            # Recovery jobs retain the real repository ID so imported works
            # appear in repository history. The active-job uniqueness guard can
            # still contain a stranded ``downloaded`` recovery row whose ledger
            # is already fully consumed (for example after an interrupted
            # operation). Close only that safe case; never supersede a genuinely
            # queued/running pipeline.
            active_job = (await db.execute(
                select(DownloadJob)
                .where(
                    DownloadJob.subscription_source_id == provisioned.subscription_source.id,
                    DownloadJob.status.in_(("enqueued", "downloading", "downloaded", "importing")),
                )
                .order_by(DownloadJob.created_at.desc(), DownloadJob.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            if active_job:
                pending_artifacts = int((await db.execute(
                    select(func.count(StorageArtifact.id)).where(
                        StorageArtifact.download_job_id == active_job.id,
                        StorageArtifact.state.in_(("new", "importing")),
                    )
                )).scalar() or 0)
                if active_job.status == "downloaded" and pending_artifacts == 0:
                    active_job.status = "complete"
                    active_job.error_log = active_job.error_log or (
                        "Closed by disk recovery after its artifact ledger was fully consumed"
                    )
                    await db.flush()
                else:
                    logger.info(
                        "disk_import: skipping %s/%s because repository job %s is still %s",
                        source,
                        creator_dir,
                        active_job.id,
                        active_job.status,
                    )
                    await db.rollback()
                    if progress_callback:
                        progress_callback({
                            "phase": "running",
                            "scanned": i + 1,
                            "total": total,
                            "source": source,
                        })
                    continue

            job = DownloadJob(
                source=source,
                source_url=provisioned.subscription_source.source_url or identity.source_url,
                status="downloaded",
                subscription_id=provisioned.subscription.id,
                subscription_source_id=provisioned.subscription_source.id,
            )
            db.add(job)
            await db.flush()

            rows = []
            seen: set[str] = set()
            new_paths: set[str] = set()
            provider = registry.get(source)
            work_groups, invalid_paths = group_metadata_by_work(provider, jsons)
            stats["skipped_invalid_metadata"] += len(invalid_paths)
            for source_work_id, items in work_groups.items():
                for jf, _ in items:
                    jr = artifact_row(
                        jf,
                        root,
                        job.id,
                        source=source,
                        creator_dir=creator_dir,
                        source_work_id=source_work_id,
                    )
                    if jr and jr["file_path"] not in seen:
                        seen.add(jr["file_path"])
                        rows.append(jr)
                        new_paths.add(str(jf))
                for asset_path in media_files_for_group(items, source_work_id):
                    ar = artifact_row(
                        asset_path,
                        root,
                        job.id,
                        source=source,
                        creator_dir=creator_dir,
                        source_work_id=source_work_id,
                    )
                    if ar and ar["file_path"] not in seen:
                        seen.add(ar["file_path"])
                        rows.append(ar)

            if not new_paths:
                await db.rollback()
                stats["skipped_invalid_metadata"] += len(jsons) - len(invalid_paths)
                continue
            await ArtifactLedger(db).upsert_many(rows)
            await db.commit()

            import_job_id = await _enqueue_import(str(job.id), new_json_paths=new_paths)
            if import_job_id:
                stats["import_job_ids"].append(import_job_id)
                if parent_task_id:
                    try:
                        from uuid import UUID
                        from app.services.tasks import TaskService
                        task = await TaskService(db).get_by_subject("import_job", UUID(import_job_id))
                        if task:
                            await TaskService(db).update_task(task, parent_task_id=UUID(str(parent_task_id)))
                            await db.commit()
                    except Exception:
                        logger.warning("disk_import: could not link child import task %s to %s", import_job_id, parent_task_id, exc_info=True)
            stats["creators"] += 1
            stats["jobs"] += 1
            if progress_callback:
                progress_callback({"phase": "running", "scanned": i + 1, "total": total, "source": source})

    return stats
