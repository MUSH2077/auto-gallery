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
from inspect import isawaitable
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.storage_artifact import StorageArtifact
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
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
    repository: SubscriptionSource | None = None
    repository_dirs: set[str] | None = None
    raw_repository_id = options.get("repository_id")
    if raw_repository_id:
        try:
            repository_id = UUID(str(raw_repository_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("repository_id must be a valid repository UUID") from exc
        repository = await db.get(SubscriptionSource, repository_id)
        if repository is None:
            raise ValueError("repository_id does not reference a repository")
        repository_source = source_key_for_extractor(repository.source)
        if source_filter and source_filter != repository_source:
            raise ValueError("source does not match repository source")
        source_filter = repository_source
        subscription = await db.get(Subscription, repository.subscription_id)
        if subscription is None:
            raise ValueError("repository has no subscription owner")
        # Task 5 owns the exact source-creator/provider identity resolution.
        # Reusing it prevents a global source scan from draining a sibling
        # repository with a lookalike directory.
        from app.services.repository_artifact_reconciliation import _repository_creator_dirs

        repository_dirs = await _repository_creator_dirs(
            db,
            repository,
            subscription.creator_id,
        )
        if not repository_dirs:
            raise ValueError("repository has no resolvable creator directory")

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
        # Stable operation counters are deliberately independent of the old
        # display-oriented names above, so a paused/retried drain can report
        # truthful resumable progress without changing older callers.
        "scanned": 0,
        "existing": 0,
        "imported": 0,
        "skipped": 0,
        "failed": 0,
    }
    parent_task_id = options.get("parent_task_id")
    # Recovery hammer: when the DB was cleared (e.g. creators deleted) but the
    # StorageArtifact ledger still marks files 'done', the normal 'done' guard
    # skips them and they never get re-imported. reset_ledger ignores the guard
    # and reprocesses everything (idempotent: import-side claim_work dedups).
    reset_ledger = bool(options.get("reset_ledger") or options.get("force"))
    stats["reset_ledger"] = reset_ledger

    async def report_progress(source: str, total: int) -> None:
        if not progress_callback:
            return
        payload = {
            "phase": "running",
            "scanned": stats["scanned"],
            "total": total,
            "source": source,
            **{
                key: stats[key]
                for key in ("existing", "imported", "skipped", "failed")
            },
        }
        outcome = progress_callback(payload)
        if isawaitable(outcome):
            await outcome

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
                stats["existing"] += 1
                continue
            if repository_dirs is not None and rel.parts[1] not in repository_dirs:
                continue
            groups[rel.parts[1]].append(jf)

        total = len(groups)
        for i, (creator_dir, jsons) in enumerate(sorted(groups.items())):
            stats["scanned"] += 1
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
                stats["skipped"] += 1
                stats["failed"] += 1
                await report_progress(source, total)
                continue

            if repository is not None and provisioned.subscription_source.id != repository.id:
                # The directory matched a provider identity but did not resolve
                # to this exact repository.  Never create a duplicate source as
                # a side effect of a scoped backlog drain.
                await db.rollback()
                stats["skipped"] += 1
                await report_progress(source, total)
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
                    stats["skipped"] += 1
                    await report_progress(source, total)
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
                stats["skipped"] += 1
                await report_progress(source, total)
                continue
            await ArtifactLedger(db).upsert_many(rows)
            if reset_ledger:
                # ``upsert_many`` deliberately preserves an unchanged ``done``
                # row.  Force recovery is the exceptional opt-in that makes the
                # exact discovered scope claimable again, under this new
                # synthetic owner; it must clear an old import lease/error too.
                await db.execute(
                    update(StorageArtifact)
                    .where(
                        StorageArtifact.storage_root == "downloads",
                        StorageArtifact.source == source,
                        StorageArtifact.file_path.in_(
                            [row["file_path"] for row in rows]
                        ),
                    )
                    .values(
                        download_job_id=job.id,
                        import_job_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        state="new",
                        last_error=None,
                    )
                )
            # Identity provisioning and the synthetic DownloadJob both change
            # creator/subscription/repository search documents.  Persist their
            # projection request in this same transaction; the asynchronous
            # drain must not sit on the disk-recovery critical path.
            from app.services.creator import CreatorService
            await CreatorService(db)._request_creator_projection(
                provisioned.creator.id,
            )
            await db.commit()

            try:
                import_job_id = await _enqueue_import(
                    str(job.id),
                    new_json_paths=new_paths,
                )
            except Exception as exc:
                # The artifact ledger was committed before publication.  Keep
                # those rows ``new`` and terminalize only this synthetic owner,
                # so a later scoped/global drain can safely recover it instead
                # of treating a temporary queue outage as lost work.
                logger.warning(
                    "disk_import: import publication failed for %s/%s",
                    source,
                    creator_dir,
                    exc_info=True,
                )
                job.status = "failed"
                job.error_log = str(exc)[:4000]
                await db.commit()
                stats["failed"] += 1
                await report_progress(source, total)
                continue
            if import_job_id:
                stats["import_job_ids"].append(import_job_id)
                stats["imported"] += 1
                if parent_task_id:
                    try:
                        from app.services.tasks import TaskService
                        task = await TaskService(db).get_by_subject("import_job", UUID(import_job_id))
                        if task:
                            await TaskService(db).update_task(task, parent_task_id=UUID(str(parent_task_id)))
                            await db.commit()
                    except Exception:
                        logger.warning("disk_import: could not link child import task %s to %s", import_job_id, parent_task_id, exc_info=True)
            stats["creators"] += 1
            stats["jobs"] += 1
            await report_progress(source, total)

    return stats
