"""Repository-scoped recovery of importable download artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_creator import SourceCreator
from app.models.download_job import DownloadJob
from app.models.storage_artifact import StorageArtifact
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.task_state import DOWNLOAD_STALE, DOWNLOAD_TERMINAL_STATUSES
from app.models.work_source import WorkSource
from app.providers import registry
from app.services.repository_identity import resolve_repository_source_creator_ids


# ``stale`` is not terminal in the task-state transition graph: a retry may
# requeue it.  It nevertheless represents a worker declared dead by the
# heartbeat owner, so its unleased ledger rows are safe historical recovery
# candidates.  Every other non-terminal, unknown, or future state fails
# closed and retains ownership.
RECOVERABLE_DOWNLOAD_OWNER_STATUSES = (
    DOWNLOAD_TERMINAL_STATUSES | frozenset({DOWNLOAD_STALE})
)


@dataclass(frozen=True, slots=True)
class RepositoryArtifactReconciliation:
    downloaded_metadata_count: int
    recovered_metadata_count: int
    pending_work_count: int
    downloaded_metadata_paths: tuple[str, ...]
    recovered_metadata_paths: tuple[str, ...]

    @property
    def metadata_paths(self) -> tuple[str, ...]:
        return self.downloaded_metadata_paths + self.recovered_metadata_paths

    @property
    def outcome_detail(self) -> dict[str, int]:
        return {
            "downloaded_metadata_count": self.downloaded_metadata_count,
            "recovered_metadata_count": self.recovered_metadata_count,
            "pending_work_count": self.pending_work_count,
        }


def _provider_creator_dir(source: str, source_url: str | None) -> str | None:
    if not source_url:
        return None
    try:
        provider = registry.get(source)
        normalized_url = provider.normalize_url(source_url) or source_url
        return provider.get_creator_dir_from_url(normalized_url)
    except (KeyError, ValueError):
        return None


async def _repository_creator_dirs(
    db: AsyncSession,
    repository: SubscriptionSource,
    creator_id: UUID,
) -> set[str]:
    """Resolve only exact repository identities to ledger creator directories."""

    identities = set(
        await resolve_repository_source_creator_ids(db, repository, creator_id)
    )
    if repository.source_creator_id:
        identities.add(repository.source_creator_id)

    directories: set[str] = set()
    repository_dir = _provider_creator_dir(repository.source, repository.source_url)
    if repository_dir:
        directories.add(repository_dir)

    if identities:
        source_creators = (
            await db.execute(
                select(SourceCreator).where(
                    SourceCreator.source == repository.source,
                    SourceCreator.source_creator_id.in_(identities),
                )
            )
        ).scalars()
        for source_creator in source_creators:
            directory = _provider_creator_dir(
                source_creator.source,
                source_creator.source_url,
            )
            if directory:
                directories.add(directory)
    return directories


def _eligible_artifact(now: datetime):
    return or_(
        StorageArtifact.state == "new",
        and_(
            StorageArtifact.state == "importing",
            or_(
                StorageArtifact.lease_expires_at.is_(None),
                StorageArtifact.lease_expires_at <= now,
            ),
        ),
    )


async def _locked_recoverable_download_owner_ids(
    db: AsyncSession,
    owner_ids: set[UUID],
) -> set[UUID]:
    """Lock candidate owners and return only their explicit safe statuses.

    Locks live until the caller commits reconciliation.  Thus an owner-status
    transition that wins first is observed before adoption, while a transition
    that follows adoption serializes behind this lock and cannot steal the
    already-adopted artifact back.
    """

    if not owner_ids:
        return set()
    owners = (
        await db.execute(
            select(DownloadJob)
            .where(DownloadJob.id.in_(owner_ids))
            .order_by(DownloadJob.id)
            .with_for_update(of=DownloadJob)
        )
    ).scalars()
    return {
        owner.id
        for owner in owners
        if owner.status in RECOVERABLE_DOWNLOAD_OWNER_STATUSES
    }


async def _locked_metadata_rows(db: AsyncSession, *conditions) -> list[StorageArtifact]:
    return list(
        (
            await db.execute(
                select(StorageArtifact)
                .where(
                    StorageArtifact.artifact_type == "metadata_json",
                    *conditions,
                )
                .order_by(StorageArtifact.created_at, StorageArtifact.id)
                .with_for_update(of=StorageArtifact, skip_locked=True)
            )
        ).scalars()
    )


async def _existing_work_ids(
    db: AsyncSession,
    source: str,
    source_work_ids: set[str],
) -> set[str]:
    if not source_work_ids:
        return set()
    return set(
        (
            await db.execute(
                select(WorkSource.source_work_id).where(
                    WorkSource.source == source,
                    WorkSource.source_work_id.in_(source_work_ids),
                )
            )
        ).scalars()
    )


async def _mark_existing_rows_done(
    db: AsyncSession,
    *,
    source: str,
    work_ids: set[str],
    conditions: tuple,
    now: datetime,
) -> None:
    if not work_ids:
        return
    await db.execute(
        update(StorageArtifact)
        .where(
            StorageArtifact.source == source,
            StorageArtifact.source_work_id.in_(work_ids),
            _eligible_artifact(now),
            *conditions,
        )
        .values(
            state="done",
            import_job_id=None,
            lease_token=None,
            lease_expires_at=None,
            last_error=None,
        )
    )


async def reconcile_repository_artifacts(
    db: AsyncSession,
    current_job,
) -> RepositoryArtifactReconciliation | None:
    """Adopt safe repository backlog into ``current_job`` without filesystem I/O.

    A standalone/manual DownloadJob has no SubscriptionSource and deliberately
    remains job-scoped.  Repository jobs use exact source-creator/provider URL
    identities, so matching source rows from another repository are excluded.
    """

    if not current_job.subscription_source_id:
        return None
    repository = await db.get(
        SubscriptionSource,
        current_job.subscription_source_id,
    )
    subscription = await db.get(Subscription, current_job.subscription_id)
    if repository is None or subscription is None:
        return None
    creator_dirs = await _repository_creator_dirs(
        db,
        repository,
        subscription.creator_id,
    )
    if not creator_dirs:
        return RepositoryArtifactReconciliation(0, 0, 0, (), ())

    now = datetime.now(timezone.utc)
    source = repository.source
    eligible = _eligible_artifact(now)
    current_rows = await _locked_metadata_rows(
        db,
        StorageArtifact.download_job_id == current_job.id,
        StorageArtifact.source == source,
        eligible,
    )
    backlog_candidates = await _locked_metadata_rows(
        db,
        StorageArtifact.download_job_id.is_distinct_from(current_job.id),
        StorageArtifact.source == source,
        StorageArtifact.creator_dir.in_(creator_dirs),
        eligible,
    )
    recoverable_owner_ids = await _locked_recoverable_download_owner_ids(
        db,
        {
            row.download_job_id
            for row in backlog_candidates
            if row.download_job_id is not None
        },
    )
    backlog_rows = [
        row
        for row in backlog_candidates
        if row.download_job_id is None or row.download_job_id in recoverable_owner_ids
    ]

    existing_ids = await _existing_work_ids(
        db,
        source,
        {row.source_work_id for row in current_rows + backlog_rows},
    )
    await _mark_existing_rows_done(
        db,
        source=source,
        work_ids={row.source_work_id for row in current_rows if row.source_work_id in existing_ids},
        conditions=(StorageArtifact.download_job_id == current_job.id,),
        now=now,
    )
    await _mark_existing_rows_done(
        db,
        source=source,
        work_ids={row.source_work_id for row in backlog_rows if row.source_work_id in existing_ids},
        conditions=(StorageArtifact.creator_dir.in_(creator_dirs),),
        now=now,
    )

    current_pending = [row for row in current_rows if row.source_work_id not in existing_ids]
    recovered = [row for row in backlog_rows if row.source_work_id not in existing_ids]
    recovered_ids = {row.source_work_id for row in recovered}
    if recovered_ids:
        await db.execute(
            update(StorageArtifact)
            .where(
                StorageArtifact.source == source,
                StorageArtifact.creator_dir.in_(creator_dirs),
                StorageArtifact.source_work_id.in_(recovered_ids),
                StorageArtifact.download_job_id.is_distinct_from(current_job.id),
                _eligible_artifact(now),
                or_(
                    StorageArtifact.download_job_id.is_(None),
                    StorageArtifact.download_job_id.in_(recoverable_owner_ids),
                ),
            )
            .values(
                download_job_id=current_job.id,
                state="new",
                import_job_id=None,
                lease_token=None,
                lease_expires_at=None,
                last_error=None,
            )
        )

    downloaded_paths = tuple(row.file_path for row in current_pending)
    recovered_paths = tuple(row.file_path for row in recovered)
    return RepositoryArtifactReconciliation(
        downloaded_metadata_count=len(downloaded_paths),
        recovered_metadata_count=len(recovered_paths),
        pending_work_count=len(
            {row.source_work_id for row in current_pending + recovered}
        ),
        downloaded_metadata_paths=downloaded_paths,
        recovered_metadata_paths=recovered_paths,
    )
