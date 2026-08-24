"""Import gallery-dl files already on disk into the DB without re-downloading.

Ordinary runs drain bounded keyset pages from the durable downloads ledger and
enqueue the normal import pipeline. Recursive filesystem discovery is reserved
for the explicit reset/untracked rebuild fallback. Idempotent ledger updates and
import-side work claims prevent duplicate imports without deleting or fetching
files again."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from inspect import isawaitable
from pathlib import Path
from uuid import UUID

from sqlalchemy import String, and_, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.storage_artifact import StorageArtifact
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.work_source import WorkSource
from app.providers import registry
from app.services.artifact_ledger import (
    ArtifactLedger,
    artifact_row,
    downloads_artifact_predicate,
)
from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group
from app.services.disk_identity import extract_metadata_identity, provision_identity_for_disk_import
from app.services.redis_pubsub import PublisherFenceError
from app.services.settings import source_key_for_extractor

logger = logging.getLogger(__name__)

DISK_IMPORT_WORK_BATCH_SIZE = 25


async def _wait_for_batch_capacity(
    parent_task_id: str | None,
    publisher_attempt: str | None = None,
) -> None:
    """Recheck host and Redis admission before each bounded publication."""

    if not parent_task_id:
        return
    from app.services.heavy_io import wait_for_resource_capacity
    from app.services.queue_admission import ensure_redis_enqueue_capacity

    await wait_for_resource_capacity(
        workload="import_db",
        owner=str(parent_task_id),
        publisher_attempt=publisher_attempt,
    )
    await asyncio.to_thread(ensure_redis_enqueue_capacity)


def _pending_download_metadata(now: datetime):
    return and_(
        downloads_artifact_predicate(),
        StorageArtifact.artifact_type == "metadata_json",
        or_(
            and_(
                StorageArtifact.state.in_(("new", "failed")),
                StorageArtifact.import_job_id.is_(None),
            ),
            and_(
                StorageArtifact.state == "importing",
                or_(
                    StorageArtifact.lease_expires_at.is_(None),
                    StorageArtifact.lease_expires_at <= now,
                ),
            ),
        ),
    )


def _download_owner_repository_key():
    """Return a total-order key for nullable repository ownership."""

    return func.coalesce(cast(DownloadJob.subscription_source_id, String), "")


def _download_owner_repository_scope(repository_id: UUID | None):
    return (
        DownloadJob.subscription_source_id == repository_id
        if repository_id is not None
        else DownloadJob.subscription_source_id.is_(None)
    )


def _recoverable_download_owner():
    from app.services.repository_artifact_reconciliation import (
        RECOVERABLE_DOWNLOAD_OWNER_STATUSES,
    )

    return or_(
        StorageArtifact.download_job_id.is_(None),
        DownloadJob.status.in_(RECOVERABLE_DOWNLOAD_OWNER_STATUSES),
    )


async def _next_pending_scope(
    db: AsyncSession,
    *,
    now: datetime,
    source_filter: str | None,
    repository_dirs: set[str] | None,
    repository_id: UUID | None,
    after: tuple[str, str, str] | None,
) -> tuple[str, str, str] | None:
    owner_key = _download_owner_repository_key()
    statement = (
        select(StorageArtifact.source, StorageArtifact.creator_dir, owner_key)
        .outerjoin(DownloadJob, DownloadJob.id == StorageArtifact.download_job_id)
        .where(_pending_download_metadata(now), _recoverable_download_owner())
    )
    if source_filter:
        statement = statement.where(StorageArtifact.source == source_filter)
    if repository_dirs is not None:
        statement = statement.where(StorageArtifact.creator_dir.in_(repository_dirs))
    if repository_id is not None:
        statement = statement.where(or_(
            StorageArtifact.download_job_id.is_(None),
            DownloadJob.subscription_source_id == repository_id,
        ))
    if after is not None:
        after_source, after_creator, after_owner = after
        statement = statement.where(or_(
            StorageArtifact.source > after_source,
            and_(
                StorageArtifact.source == after_source,
                StorageArtifact.creator_dir > after_creator,
            ),
            and_(
                StorageArtifact.source == after_source,
                StorageArtifact.creator_dir == after_creator,
                owner_key > after_owner,
            ),
        ))
    row = (
        await db.execute(
            statement
            .group_by(StorageArtifact.source, StorageArtifact.creator_dir, owner_key)
            .order_by(StorageArtifact.source, StorageArtifact.creator_dir, owner_key)
            .limit(1)
        )
    ).first()
    return tuple(str(value) for value in row) if row else None


async def _pending_work_page(
    db: AsyncSession,
    *,
    now: datetime,
    source: str,
    creator_dir: str,
    owner_repository_id: UUID | None,
    after_work_id: str | None,
) -> list[str]:
    statement = (
        select(StorageArtifact.source_work_id)
        .outerjoin(DownloadJob, DownloadJob.id == StorageArtifact.download_job_id)
        .where(
            _pending_download_metadata(now),
            _recoverable_download_owner(),
            StorageArtifact.source == source,
            StorageArtifact.creator_dir == creator_dir,
            _download_owner_repository_scope(owner_repository_id),
        )
    )
    if after_work_id is not None:
        statement = statement.where(StorageArtifact.source_work_id > after_work_id)
    return list((await db.execute(
        statement
        .group_by(StorageArtifact.source_work_id)
        .order_by(StorageArtifact.source_work_id)
        .limit(DISK_IMPORT_WORK_BATCH_SIZE)
    )).scalars())


async def _resolve_ledger_scope_repository(
    db: AsyncSession,
    *,
    root: Path,
    source: str,
    creator_dir: str,
    work_ids: list[str],
    owner_repository_id: UUID | None,
    repository: SubscriptionSource | None,
):
    if repository is not None:
        subscription = await db.get(Subscription, repository.subscription_id)
        if subscription is None:
            raise ValueError("repository has no subscription owner")
        return repository, subscription

    if owner_repository_id is not None:
        resolved_repository = await db.get(SubscriptionSource, owner_repository_id)
        if resolved_repository is not None:
            subscription = await db.get(
                Subscription,
                resolved_repository.subscription_id,
            )
            if subscription is not None:
                return resolved_repository, subscription

    metadata_path = (
        await db.execute(
            select(StorageArtifact.file_path)
            .outerjoin(
                DownloadJob,
                DownloadJob.id == StorageArtifact.download_job_id,
            )
            .where(
                _pending_download_metadata(datetime.now(timezone.utc)),
                StorageArtifact.source == source,
                StorageArtifact.creator_dir == creator_dir,
                StorageArtifact.source_work_id.in_(work_ids),
                DownloadJob.subscription_source_id.is_(None),
            )
            .order_by(StorageArtifact.created_at, StorageArtifact.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if metadata_path is None:
        raise ValueError("ledger scope has no metadata path")
    with open(root / metadata_path) as metadata_file:
        identity = extract_metadata_identity(
            source,
            json.load(metadata_file),
            creator_dir,
        )
    provisioned = await provision_identity_for_disk_import(db, identity)
    from app.services.creator import CreatorService
    await CreatorService(db)._request_creator_projection(provisioned.creator.id)
    return provisioned.subscription_source, provisioned.subscription


async def _drain_pending_ledger(
    db: AsyncSession,
    *,
    root: Path,
    source_filter: str | None,
    repository: SubscriptionSource | None,
    repository_dirs: set[str] | None,
    parent_task_id: str | None,
    stats: dict,
    progress_callback,
    enqueue_import,
    publisher_checkpoint=None,
    publisher_attempt: str | None = None,
) -> dict:
    """Publish bounded keyset pages from the durable downloads ledger."""

    from app.services.repository_artifact_reconciliation import (
        _locked_recoverable_download_owner_ids,
    )

    seen_sources: set[str] = set()
    scope_cursor: tuple[str, str, str] | None = None

    async def checkpoint(*, lock_task: bool = False) -> None:
        if publisher_checkpoint is None:
            return
        outcome = publisher_checkpoint(db, lock_task=lock_task)
        if isawaitable(outcome):
            await outcome

    async def report(source: str) -> None:
        if not progress_callback:
            return
        await checkpoint()
        payload = {
            "phase": "running",
            "scanned": stats["scanned"],
            "total": stats["scanned"],
            "source": source,
            **{
                key: stats[key]
                for key in ("existing", "imported", "skipped", "failed")
            },
        }
        outcome = progress_callback(payload)
        if isawaitable(outcome):
            await outcome

    while True:
        await checkpoint()
        now = datetime.now(timezone.utc)
        scope = await _next_pending_scope(
            db,
            now=now,
            source_filter=source_filter,
            repository_dirs=repository_dirs,
            repository_id=repository.id if repository is not None else None,
            after=scope_cursor,
        )
        if scope is None:
            break
        scope_cursor = scope
        source, creator_dir, owner_repository_key = scope
        owner_repository_id = (
            UUID(owner_repository_key) if owner_repository_key else None
        )
        try:
            registry.get(source)
        except KeyError:
            stats["skipped"] += 1
            await report(source)
            continue

        seen_sources.add(source)
        stats["sources"] = len(seen_sources)
        work_cursor: str | None = None
        recovery_job: DownloadJob | None = None
        imported_scope = False
        while True:
            await checkpoint()
            now = datetime.now(timezone.utc)
            work_ids = await _pending_work_page(
                db,
                now=now,
                source=source,
                creator_dir=creator_dir,
                owner_repository_id=owner_repository_id,
                after_work_id=work_cursor,
            )
            if not work_ids:
                break
            work_cursor = work_ids[-1]
            stats["scanned"] += len(work_ids)
            await _wait_for_batch_capacity(
                parent_task_id,
                **(
                    {"publisher_attempt": publisher_attempt}
                    if publisher_attempt is not None
                    else {}
                ),
            )
            await checkpoint()

            try:
                resolved_repository, subscription = (
                    await _resolve_ledger_scope_repository(
                        db,
                        root=root,
                        source=source,
                        creator_dir=creator_dir,
                        work_ids=work_ids,
                        owner_repository_id=owner_repository_id,
                        repository=repository,
                    )
                )
            except Exception:
                logger.warning(
                    "disk_import: could not resolve ledger scope %s/%s",
                    source,
                    creator_dir,
                    exc_info=True,
                )
                await db.rollback()
                stats["failed"] += len(work_ids)
                stats["skipped"] += len(work_ids)
                await report(source)
                continue

            rows = list((await db.execute(
                select(StorageArtifact)
                .outerjoin(
                    DownloadJob,
                    DownloadJob.id == StorageArtifact.download_job_id,
                )
                .where(
                    downloads_artifact_predicate(),
                    StorageArtifact.source == source,
                    StorageArtifact.creator_dir == creator_dir,
                    StorageArtifact.source_work_id.in_(work_ids),
                    _download_owner_repository_scope(owner_repository_id),
                    or_(
                        StorageArtifact.state.in_(("new", "failed")),
                        and_(
                            StorageArtifact.state == "importing",
                            or_(
                                StorageArtifact.lease_expires_at.is_(None),
                                StorageArtifact.lease_expires_at <= now,
                            ),
                        ),
                    ),
                )
                .order_by(
                    StorageArtifact.download_job_id.asc(),
                    StorageArtifact.created_at,
                    StorageArtifact.id,
                )
                .with_for_update(of=StorageArtifact, skip_locked=True)
            )).scalars())
            owner_ids = {
                row.download_job_id
                for row in rows
                if row.download_job_id is not None
            }
            if recovery_job is not None:
                owner_ids.add(recovery_job.id)
            recoverable_owner_ids = await _locked_recoverable_download_owner_ids(
                db,
                owner_ids,
            )
            claimable_rows = [
                row
                for row in rows
                if row.download_job_id is None
                or row.download_job_id in recoverable_owner_ids
                or (recovery_job is not None and row.download_job_id == recovery_job.id)
            ]
            claimable_work_ids = {row.source_work_id for row in claimable_rows}
            existing_ids = set((await db.execute(
                select(WorkSource.source_work_id).where(
                    WorkSource.source == source,
                    WorkSource.source_work_id.in_(claimable_work_ids),
                )
            )).scalars())
            await checkpoint(lock_task=True)
            if existing_ids:
                await db.execute(
                    update(StorageArtifact)
                    .where(
                        downloads_artifact_predicate(),
                        StorageArtifact.id.in_({
                            row.id
                            for row in claimable_rows
                            if row.source_work_id in existing_ids
                        }),
                    )
                    .values(
                        state="done",
                        import_job_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=None,
                    )
                )
                stats["existing"] += len(existing_ids)

            metadata_by_work: dict[str, list[Path]] = defaultdict(list)
            for row in claimable_rows:
                if (
                    row.source_work_id not in existing_ids
                    and row.artifact_type == "metadata_json"
                ):
                    path = root / row.file_path
                    if path.is_file():
                        metadata_by_work[row.source_work_id].append(path)
            importable_work_ids = set(metadata_by_work)
            missing_ids = claimable_work_ids - existing_ids - importable_work_ids
            if missing_ids:
                await db.execute(
                    update(StorageArtifact)
                    .where(
                        downloads_artifact_predicate(),
                        StorageArtifact.id.in_({
                            row.id
                            for row in claimable_rows
                            if row.source_work_id in missing_ids
                        }),
                    )
                    .values(
                        state="failed",
                        import_job_id=None,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error="Metadata file is missing from DOWNLOAD_ROOT",
                    )
                )
                stats["failed"] += len(missing_ids)
                stats["skipped"] += len(missing_ids)

            if not importable_work_ids:
                await checkpoint(lock_task=True)
                await db.commit()
                await report(source)
                continue

            if recovery_job is None:
                recovery_job = DownloadJob(
                    source=source,
                    source_url=(
                        resolved_repository.source_url
                        or f"{source}:{creator_dir}"
                    ),
                    status="downloaded",
                    subscription_id=subscription.id,
                    subscription_source_id=resolved_repository.id,
                    manifest={
                        "disk_import_recovery": True,
                        "bounded_import_publication_open": True,
                        "bounded_import_publisher_task_id": (
                            str(parent_task_id) if parent_task_id else None
                        ),
                        "bounded_import_publisher_started_at": now.isoformat(),
                    },
                )
                db.add(recovery_job)
                await db.flush()
                stats["jobs"] += 1

            importable_row_ids = {
                row.id
                for row in claimable_rows
                if row.source_work_id in importable_work_ids
            }
            await db.execute(
                update(StorageArtifact)
                .where(
                    downloads_artifact_predicate(),
                    StorageArtifact.id.in_(importable_row_ids),
                )
                .values(
                    download_job_id=recovery_job.id,
                    state="new",
                    import_job_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=None,
                )
            )
            await checkpoint(lock_task=True)
            await db.commit()

            metadata_paths = {
                str(path)
                for work_id in sorted(importable_work_ids)
                for path in metadata_by_work[work_id]
            }
            try:
                await checkpoint()
                enqueue_kwargs = {"new_json_paths": metadata_paths}
                if publisher_checkpoint is not None:
                    enqueue_kwargs["publisher_checkpoint"] = publisher_checkpoint
                import_job_id = await enqueue_import(
                    str(recovery_job.id),
                    **enqueue_kwargs,
                )
            except PublisherFenceError:
                raise
            except Exception as exc:
                logger.warning(
                    "disk_import: import publication failed for %s/%s",
                    source,
                    creator_dir,
                    exc_info=True,
                )
                recovery_job = (
                    await db.execute(
                        select(DownloadJob)
                        .where(DownloadJob.id == recovery_job.id)
                        .with_for_update(of=DownloadJob)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                await checkpoint(lock_task=True)
                recovery_job.status = "failed"
                recovery_job.error_log = str(exc)[:4000]
                await db.commit()
                stats["failed"] += len(importable_work_ids)
                await report(source)
                return stats

            if import_job_id:
                stats["import_job_ids"].append(import_job_id)
                stats["imported"] += 1
                if parent_task_id:
                    try:
                        from app.services.tasks import TaskService

                        task = await TaskService(db).get_by_subject(
                            "import_job",
                            UUID(str(import_job_id)),
                        )
                        if task:
                            await checkpoint(lock_task=True)
                            await TaskService(db).update_task(
                                task,
                                parent_task_id=UUID(str(parent_task_id)),
                            )
                            await db.commit()
                    except (TypeError, ValueError):
                        logger.debug(
                            "disk_import: test/legacy import id is not a UUID: %s",
                            import_job_id,
                        )
                    except Exception:
                        logger.warning(
                            "disk_import: could not link child import task %s to %s",
                            import_job_id,
                            parent_task_id,
                            exc_info=True,
                        )
            imported_scope = True
            await report(source)

        if recovery_job is not None:
            from app.services.import_lifecycle import (
                close_bounded_import_publication,
            )

            await checkpoint()
            completion = await close_bounded_import_publication(
                db,
                recovery_job.id,
                publisher_checkpoint=publisher_checkpoint,
            )
            if completion is not None and completion.should_finalize:
                from app.services.download_finalization import (
                    finalize_download_job,
                )
                from app.services.sync_outcome import build_sync_outcome

                outcome = (
                    build_sync_outcome(
                        (
                            "new_content"
                            if completion.stats["works"] > 0
                            else "no_changes"
                        ),
                        metadata_count=completion.total_groups,
                        media_count=completion.stats["assets"],
                    )
                    if completion.status == "complete"
                    else None
                )
                await finalize_download_job(
                    db,
                    completion.parent,
                    status=completion.status,
                    outcome=outcome,
                    error=(
                        completion.message
                        if completion.status == "failed"
                        else None
                    ),
                    message=completion.message,
                    assets=completion.stats["assets"],
                )
            else:
                await checkpoint(lock_task=True)
                await db.commit()

        if imported_scope:
            stats["creators"] += 1

    return stats

async def reconcile_downloads_to_db(
    db: AsyncSession,
    options: dict,
    progress_callback=None,
    *,
    publisher_checkpoint=None,
    publisher_attempt: str | None = None,
) -> dict:
    """Import on-disk download files (not yet imported) into the DB. Idempotent."""
    from app.jobs.download import _enqueue_import

    async def checkpoint(*, lock_task: bool = False) -> None:
        if publisher_checkpoint is None:
            return
        outcome = publisher_checkpoint(db, lock_task=lock_task)
        if isawaitable(outcome):
            await outcome

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
        await checkpoint()
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

    if not reset_ledger:
        return await _drain_pending_ledger(
            db,
            root=root,
            source_filter=source_filter,
            repository=repository,
            repository_dirs=repository_dirs,
            parent_task_id=parent_task_id,
            stats=stats,
            progress_callback=progress_callback,
            enqueue_import=_enqueue_import,
            publisher_checkpoint=publisher_checkpoint,
            publisher_attempt=publisher_attempt,
        )

    # Recursive discovery is intentionally confined to the explicit reset /
    # untracked-file rebuild fallback. Repository reset scans only its resolved
    # directories and never the source's sibling creator trees.
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

    for disk_source, source in sources:
        await checkpoint()
        stats["sources"] += 1
        scan_root = root / disk_source

        done_paths = set()

        groups: dict[str, list[Path]] = defaultdict(list)
        discovery_roots = (
            [scan_root / directory for directory in sorted(repository_dirs)]
            if repository_dirs is not None
            else [scan_root]
        )
        for discovery_root in discovery_roots:
            if not discovery_root.is_dir():
                continue
            for jf in discovery_root.rglob("*.json"):
                if not jf.is_file() or jf.parent == scan_root:
                    continue
                rel = jf.relative_to(root)
                if len(rel.parts) < 3:
                    continue
                if str(rel) in done_paths:
                    stats["skipped_done"] += 1
                    stats["existing"] += 1
                    continue
                groups[rel.parts[1]].append(jf)

        total = len(groups)
        for i, (creator_dir, jsons) in enumerate(sorted(groups.items())):
            await checkpoint()
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
                        downloads_artifact_predicate(),
                        StorageArtifact.download_job_id == active_job.id,
                        StorageArtifact.state.in_(("new", "importing")),
                    )
                )).scalar() or 0)
                if active_job.status == "downloaded" and pending_artifacts == 0:
                    await checkpoint()
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
            await checkpoint()
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
            await checkpoint(lock_task=True)
            await db.commit()

            try:
                await checkpoint()
                enqueue_kwargs = {"new_json_paths": new_paths}
                if publisher_checkpoint is not None:
                    enqueue_kwargs["publisher_checkpoint"] = publisher_checkpoint
                import_job_id = await _enqueue_import(
                    str(job.id),
                    **enqueue_kwargs,
                )
            except PublisherFenceError:
                raise
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
                job = (
                    await db.execute(
                        select(DownloadJob)
                        .where(DownloadJob.id == job.id)
                        .with_for_update(of=DownloadJob)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                await checkpoint(lock_task=True)
                job.status = "failed"
                job.error_log = str(exc)[:4000]
                await checkpoint()
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
                            await checkpoint(lock_task=True)
                            await TaskService(db).update_task(task, parent_task_id=UUID(str(parent_task_id)))
                            await checkpoint()
                            await db.commit()
                    except Exception:
                        logger.warning("disk_import: could not link child import task %s to %s", import_job_id, parent_task_id, exc_info=True)
            stats["creators"] += 1
            stats["jobs"] += 1
            await report_progress(source, total)

    return stats
