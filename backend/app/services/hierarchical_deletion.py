from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, delete as sql_delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetSource,
    Creator,
    CreatorCurationState,
    CreatorLink,
    DownloadJob,
    ImportJob,
    RepositorySyncReceipt,
    SourceCreator,
    Subscription,
    SubscriptionSource,
    TaskRun,
    Work,
    WorkCurationState,
    WorkSource,
)
from app.models.task_state import DOWNLOAD_RUNNING_STATUSES
from app.services.curation import CurationService
from app.services.repository_identity import resolve_repository_source_creator_ids
from app.services.search_projection_outbox import request_search_projection


DeletionEntityType = Literal["repository", "subscription", "creator"]
ACTIVE_DELETE_BLOCKING_DOWNLOAD_STATUSES = frozenset(
    {*DOWNLOAD_RUNNING_STATUSES, "pending", "paused", "recovering"}
)
ACTIVE_DELETE_BLOCKING_TASK_STATUSES = frozenset(
    {
        "enqueued",
        "running",
        "downloading",
        "downloaded",
        "importing",
        "paused",
        "recovering",
    }
)


def _identity(source: str, source_creator_id: str) -> tuple[str, str]:
    normalized_source = source.strip().casefold()
    if normalized_source == "twitter":
        normalized_source = "x"
    return normalized_source, source_creator_id


def _source_matches(column, source: str):
    if source == "x":
        return func.lower(column).in_(("x", "twitter"))
    return func.lower(column) == source


@dataclass(slots=True)
class DeletionScope:
    entity_type: DeletionEntityType
    entity_ids: set[UUID]
    creator_ids: set[UUID] = field(default_factory=set)
    subscription_ids: set[UUID] = field(default_factory=set)
    repository_ids: set[UUID] = field(default_factory=set)
    identities: set[tuple[str, str]] = field(default_factory=set)
    affected_work_ids: set[UUID] = field(default_factory=set)
    exclusive_work_ids: set[UUID] = field(default_factory=set)
    shared_work_ids: set[UUID] = field(default_factory=set)
    exclusive_asset_ids: set[UUID] = field(default_factory=set)
    active_task_ids: set[UUID] = field(default_factory=set)
    active_job_count: int = 0

    def preview_payload(self, *, is_admin: bool) -> dict:
        return {
            "entity_type": self.entity_type,
            "entity_ids": sorted(self.entity_ids, key=str),
            "mode": "permanent" if is_admin else "soft",
            "can_delete_files": is_admin,
            "active_task_count": max(
                self.active_job_count,
                len(self.active_task_ids),
            ),
            "active_job_count": self.active_job_count,
            "active_task_ids": sorted(self.active_task_ids, key=str),
            "affected_work_count": len(self.affected_work_ids),
            "exclusive_work_count": len(self.exclusive_work_ids),
            "shared_work_count": len(self.shared_work_ids),
            "exclusive_asset_count": len(self.exclusive_asset_ids),
        }


ProgressCallback = Callable[[int, int, str], Awaitable[None]]


class HierarchicalDeletionService:
    """Permission-aware deletion for repository -> subscription -> creator.

    Work ownership is deliberately derived from paired source identities.  A
    work is exclusive only when every WorkSource belongs to this deletion and
    none of those identities is still claimed by a surviving repository.
    Ambiguous/null creator identities are treated as shared.
    """

    WORK_BATCH_SIZE = CurationService.COMMIT_WORK_LIMIT

    def __init__(self, db: AsyncSession):
        self.db = db

    async def scope(
        self,
        entity_type: DeletionEntityType,
        entity_ids: list[UUID] | set[UUID],
    ) -> DeletionScope:
        ids = set(entity_ids)
        if not ids:
            raise HTTPException(status_code=422, detail="at least one deletion target is required")
        scope = DeletionScope(entity_type=entity_type, entity_ids=ids)
        await self._load_targets(scope)
        await self._load_identities(scope)
        await self._load_work_scope(scope)
        await self._load_active_jobs(scope)
        return scope

    async def _load_targets(self, scope: DeletionScope) -> None:
        if scope.entity_type == "repository":
            rows = list((await self.db.execute(
                select(SubscriptionSource.id, SubscriptionSource.subscription_id, Subscription.creator_id)
                .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                .where(SubscriptionSource.id.in_(scope.entity_ids))
            )).all())
            found = {row.id for row in rows}
            scope.repository_ids = found
            scope.subscription_ids = {row.subscription_id for row in rows}
            scope.creator_ids = {row.creator_id for row in rows}
        elif scope.entity_type == "subscription":
            rows = list((await self.db.execute(
                select(Subscription.id, Subscription.creator_id)
                .where(Subscription.id.in_(scope.entity_ids))
            )).all())
            found = {row.id for row in rows}
            scope.subscription_ids = found
            scope.creator_ids = {row.creator_id for row in rows}
            scope.repository_ids = set((await self.db.execute(
                select(SubscriptionSource.id).where(
                    SubscriptionSource.subscription_id.in_(scope.subscription_ids)
                )
            )).scalars().all())
        else:
            found = set((await self.db.execute(
                select(Creator.id).where(Creator.id.in_(scope.entity_ids))
            )).scalars().all())
            scope.creator_ids = found
            scope.subscription_ids = set((await self.db.execute(
                select(Subscription.id).where(Subscription.creator_id.in_(scope.creator_ids))
            )).scalars().all())
            if scope.subscription_ids:
                scope.repository_ids = set((await self.db.execute(
                    select(SubscriptionSource.id).where(
                        SubscriptionSource.subscription_id.in_(scope.subscription_ids)
                    )
                )).scalars().all())

        missing = scope.entity_ids - found
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "deletion_target_not_found",
                    "entity_type": scope.entity_type,
                    "ids": [str(value) for value in sorted(missing, key=str)],
                },
            )

    async def _load_identities(self, scope: DeletionScope) -> None:
        if scope.entity_type == "creator" and scope.creator_ids:
            creator_rows = (await self.db.execute(
                select(SourceCreator.source, SourceCreator.source_creator_id).where(
                    SourceCreator.creator_id.in_(scope.creator_ids)
                )
            )).all()
            scope.identities.update(
                _identity(row.source, row.source_creator_id) for row in creator_rows
            )

        if not scope.repository_ids:
            return
        repository_rows = list((await self.db.execute(
            select(SubscriptionSource, Subscription.creator_id)
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .where(SubscriptionSource.id.in_(scope.repository_ids))
        )).all())
        for repository, creator_id in repository_rows:
            if repository.source_creator_id:
                scope.identities.add(
                    _identity(repository.source, repository.source_creator_id)
                )
                continue
            for source_creator_id in await resolve_repository_source_creator_ids(
                self.db, repository, creator_id
            ):
                scope.identities.add(_identity(repository.source, source_creator_id))

    async def _load_work_scope(self, scope: DeletionScope) -> None:
        if not scope.identities:
            return

        identity_predicates = [
            and_(
                _source_matches(WorkSource.source, source),
                WorkSource.source_creator_id == source_creator_id,
            )
            for source, source_creator_id in scope.identities
        ]
        affected = set((await self.db.execute(
            select(WorkSource.work_id).where(or_(*identity_predicates)).distinct()
        )).scalars().all())
        scope.affected_work_ids = affected
        if not affected:
            return

        surviving_claims: set[tuple[str, str]] = set()
        surviving_repo_filters = [SubscriptionSource.source_creator_id.is_not(None)]
        if scope.repository_ids:
            surviving_repo_filters.append(~SubscriptionSource.id.in_(scope.repository_ids))
        repo_claims = (await self.db.execute(
            select(SubscriptionSource.source, SubscriptionSource.source_creator_id).where(
                *surviving_repo_filters
            )
        )).all()
        surviving_claims.update(
            _identity(row.source, row.source_creator_id) for row in repo_claims
            if row.source_creator_id
        )
        creator_claim_filters = [SourceCreator.creator_id.is_not(None)]
        if scope.entity_type == "creator" and scope.creator_ids:
            creator_claim_filters.append(~SourceCreator.creator_id.in_(scope.creator_ids))
        creator_claims = (await self.db.execute(
            select(SourceCreator.source, SourceCreator.source_creator_id).where(
                *creator_claim_filters
            )
        )).all()
        if creator_claims:
            surviving_claims.update(
                _identity(row.source, row.source_creator_id) for row in creator_claims
            )

        deletable_identities = scope.identities - surviving_claims
        sources_by_work: dict[UUID, list[tuple[str, str] | None]] = {
            work_id: [] for work_id in affected
        }
        work_source_rows = (await self.db.execute(
            select(
                WorkSource.work_id,
                WorkSource.source,
                WorkSource.source_creator_id,
            ).where(WorkSource.work_id.in_(affected))
        )).all()
        for row in work_source_rows:
            sources_by_work[row.work_id].append(
                _identity(row.source, row.source_creator_id)
                if row.source_creator_id else None
            )

        scope.exclusive_work_ids = {
            work_id
            for work_id, identities in sources_by_work.items()
            if identities
            and all(identity is not None and identity in deletable_identities for identity in identities)
        }
        scope.shared_work_ids = affected - scope.exclusive_work_ids
        scope.exclusive_asset_ids = await self._exclusive_assets(scope.exclusive_work_ids)

    async def _exclusive_assets(self, work_ids: set[UUID]) -> set[UUID]:
        if not work_ids:
            return set()
        candidate_asset_ids = set((await self.db.execute(
            select(AssetSource.asset_id)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .where(WorkSource.work_id.in_(work_ids))
            .distinct()
        )).scalars().all())
        candidate_asset_ids.update((await self.db.execute(
            select(Work.thumbnail_asset_id).where(
                Work.id.in_(work_ids),
                Work.thumbnail_asset_id.is_not(None),
            )
        )).scalars().all())
        if not candidate_asset_ids:
            return set()
        shared_asset_ids = set((await self.db.execute(
            select(AssetSource.asset_id)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .where(
                AssetSource.asset_id.in_(candidate_asset_ids),
                ~WorkSource.work_id.in_(work_ids),
            )
            .distinct()
        )).scalars().all())
        shared_asset_ids.update((await self.db.execute(
            select(Work.thumbnail_asset_id).where(
                Work.thumbnail_asset_id.in_(candidate_asset_ids),
                ~Work.id.in_(work_ids),
            )
        )).scalars().all())
        return candidate_asset_ids - shared_asset_ids

    async def _load_active_jobs(self, scope: DeletionScope) -> None:
        job_filters = []
        if scope.repository_ids:
            job_filters.append(DownloadJob.subscription_source_id.in_(scope.repository_ids))
        if scope.entity_type in {"subscription", "creator"} and scope.subscription_ids:
            job_filters.append(DownloadJob.subscription_id.in_(scope.subscription_ids))
        if not job_filters:
            return
        download_rows = (await self.db.execute(
            select(DownloadJob.id, DownloadJob.status).where(or_(*job_filters))
        )).all()
        all_download_ids = {row.id for row in download_rows}
        active_download_ids = {
            row.id
            for row in download_rows
            if row.status in ACTIVE_DELETE_BLOCKING_DOWNLOAD_STATUSES
        }
        import_rows = []
        if all_download_ids:
            import_rows = list((await self.db.execute(
                select(ImportJob.id, ImportJob.status).where(
                    ImportJob.download_job_id.in_(all_download_ids)
                )
            )).all())
        all_import_ids = {row.id for row in import_rows}
        active_import_ids = {
            row.id
            for row in import_rows
            if row.status in ACTIVE_DELETE_BLOCKING_TASK_STATUSES
        }

        task_predicates = []
        if all_download_ids:
            task_predicates.append(and_(
                TaskRun.subject_type == "download_job",
                TaskRun.subject_id.in_(all_download_ids),
            ))
        if all_import_ids:
            task_predicates.append(and_(
                TaskRun.subject_type == "import_job",
                TaskRun.subject_id.in_(all_import_ids),
            ))
        if task_predicates:
            scope.active_task_ids = set((await self.db.execute(
                select(TaskRun.id).where(
                    or_(*task_predicates),
                    TaskRun.status.in_(ACTIVE_DELETE_BLOCKING_TASK_STATUSES),
                )
            )).scalars().all())
        scope.active_job_count = len(active_download_ids) + len(active_import_ids)
        if not scope.active_task_ids:
            scope.active_task_ids = active_download_ids | active_import_ids

    @staticmethod
    def ensure_no_active_jobs(scope: DeletionScope) -> None:
        if not scope.active_job_count and not scope.active_task_ids:
            return
        raise HTTPException(
            status_code=409,
            detail={
                "code": "deletion_active_tasks",
                "message": "Stop or cancel active tasks before deleting this target.",
                "entity_type": scope.entity_type,
                "active_job_count": scope.active_job_count,
                "task_ids": [str(value) for value in sorted(scope.active_task_ids, key=str)],
            },
        )

    async def soft_delete(self, scope: DeletionScope) -> dict:
        self.ensure_no_active_jobs(scope)
        if scope.entity_type == "repository":
            rows = list((await self.db.execute(
                select(SubscriptionSource).where(
                    SubscriptionSource.id.in_(scope.repository_ids)
                )
            )).scalars().all())
            for row in rows:
                row.is_enabled = False
                row.next_sync_at = None
            await request_search_projection(
                self.db,
                scope.affected_work_ids,
                creator_ids=scope.creator_ids,
                repository_ids=scope.repository_ids,
                subscription_ids=scope.subscription_ids,
            )
            await self.db.commit()
        elif scope.entity_type == "subscription":
            rows = list((await self.db.execute(
                select(Subscription).where(Subscription.id.in_(scope.subscription_ids))
            )).scalars().all())
            for row in rows:
                row.is_active = False
            await request_search_projection(
                self.db,
                scope.affected_work_ids,
                creator_ids=scope.creator_ids,
                repository_ids=scope.repository_ids,
                subscription_ids=scope.subscription_ids,
            )
            await self.db.commit()
        else:
            subscriptions = list((await self.db.execute(
                select(Subscription).where(
                    Subscription.id.in_(scope.subscription_ids)
                )
            )).scalars().all())
            for subscription in subscriptions:
                subscription.is_active = False
            await request_search_projection(
                self.db,
                subscription_ids=scope.subscription_ids,
                repository_ids=scope.repository_ids,
            )
            await self.db.commit()
            curation = CurationService(self.db)
            for creator_id in sorted(scope.creator_ids, key=str):
                await curation.curate_creator(
                    creator_id,
                    action="archive",
                    reason="hierarchy soft delete",
                    work_ids=[],
                )
            for chunk in self._chunks(scope.exclusive_work_ids):
                await curation.trash_works(
                    chunk,
                    reason="creator soft deleted",
                    message="Move creator-exclusive works to trash",
                    trigger="creator_archive_works",
                )
        return {
            "status": "soft_deleted",
            "mode": "soft",
            "entity_type": scope.entity_type,
            "entity_ids": sorted(scope.entity_ids, key=str),
            "delete_files": False,
            "message": "Target moved to an inactive, recoverable state.",
        }

    async def permanent_delete(
        self,
        scope: DeletionScope,
        *,
        delete_files: bool,
        progress: ProgressCallback | None = None,
    ) -> dict:
        self.ensure_no_active_jobs(scope)
        await self._disable_targets(scope)
        # Disabling closes the scheduler/manual-sync admission path. Rebuild
        # the scope afterwards so a task admitted between the preview and the
        # worker start is still rejected before any content is curated.
        scope = await self.scope(scope.entity_type, scope.entity_ids)
        self.ensure_no_active_jobs(scope)

        batches = list(self._chunks(scope.exclusive_work_ids))
        total = len(scope.exclusive_work_ids)
        processed = 0
        curation = CurationService(self.db)
        for chunk in batches:
            state_rows = (await self.db.execute(
                select(WorkCurationState.work_id, WorkCurationState.visibility).where(
                    WorkCurationState.work_id.in_(chunk)
                )
            )).all()
            states = {row.work_id: row.visibility for row in state_rows}
            visible_ids = [
                work_id for work_id in chunk
                if states.get(work_id, "visible") == "visible"
            ]
            if visible_ids:
                await curation.trash_works(
                    visible_ids,
                    reason=f"{scope.entity_type} permanently deleted",
                    message=f"Trash works before deleting {scope.entity_type}",
                    trigger="hierarchy_delete_trash",
                )
            if delete_files:
                purge_ids = [
                    work_id for work_id in chunk
                    if states.get(work_id, "visible") != "purged"
                ]
                if purge_ids:
                    await curation.purge(
                        purge_ids,
                        message=f"Purge exclusive works for deleted {scope.entity_type}",
                        strict_file_cleanup=True,
                        asset_scope_work_ids=list(scope.exclusive_work_ids),
                    )
            processed += len(chunk)
            if progress:
                await progress(processed, total, "Deleting exclusive works")

        await self._delete_domain_rows(scope)
        return {
            "status": "complete",
            "message": f"Deleted {len(scope.entity_ids)} {scope.entity_type} target(s)",
            "entity_type": scope.entity_type,
            "entity_ids": [str(value) for value in sorted(scope.entity_ids, key=str)],
            "delete_files": delete_files,
            "affected_works": len(scope.affected_work_ids),
            "trashed_or_purged_works": len(scope.exclusive_work_ids),
            "shared_works_preserved": len(scope.shared_work_ids),
            "exclusive_assets": len(scope.exclusive_asset_ids),
        }

    async def _disable_targets(self, scope: DeletionScope) -> None:
        if scope.repository_ids:
            repositories = list((await self.db.execute(
                select(SubscriptionSource).where(
                    SubscriptionSource.id.in_(scope.repository_ids)
                )
            )).scalars().all())
            for repository in repositories:
                repository.is_enabled = False
                repository.next_sync_at = None
        if scope.entity_type in {"subscription", "creator"} and scope.subscription_ids:
            subscriptions = list((await self.db.execute(
                select(Subscription).where(Subscription.id.in_(scope.subscription_ids))
            )).scalars().all())
            for subscription in subscriptions:
                subscription.is_active = False
        if scope.entity_type == "creator":
            curation = CurationService(self.db)
            for creator_id in sorted(scope.creator_ids, key=str):
                await curation.curate_creator(
                    creator_id,
                    action="archive",
                    reason="pending permanent deletion",
                    work_ids=[],
                )
        else:
            await self.db.commit()

    async def _delete_domain_rows(self, scope: DeletionScope) -> None:
        download_filters = []
        if scope.repository_ids:
            download_filters.append(DownloadJob.subscription_source_id.in_(scope.repository_ids))
        if scope.entity_type in {"subscription", "creator"} and scope.subscription_ids:
            download_filters.append(DownloadJob.subscription_id.in_(scope.subscription_ids))
        download_ids: set[UUID] = set()
        if download_filters:
            download_ids = set((await self.db.execute(
                select(DownloadJob.id).where(or_(*download_filters))
            )).scalars().all())
        if download_ids:
            await self.db.execute(
                sql_delete(ImportJob).where(ImportJob.download_job_id.in_(download_ids))
            )
            await self.db.execute(
                sql_delete(DownloadJob).where(DownloadJob.id.in_(download_ids))
            )

        if scope.repository_ids:
            await self.db.execute(
                sql_delete(RepositorySyncReceipt).where(
                    RepositorySyncReceipt.repository_id.in_(scope.repository_ids)
                )
            )
            await self.db.execute(
                sql_delete(SubscriptionSource).where(
                    SubscriptionSource.id.in_(scope.repository_ids)
                )
            )
        if scope.entity_type in {"subscription", "creator"} and scope.subscription_ids:
            await self.db.execute(
                sql_delete(Subscription).where(Subscription.id.in_(scope.subscription_ids))
            )
        if scope.entity_type == "creator":
            await self.db.execute(
                sql_delete(SourceCreator).where(SourceCreator.creator_id.in_(scope.creator_ids))
            )
            await self.db.execute(
                sql_delete(CreatorLink).where(CreatorLink.creator_id.in_(scope.creator_ids))
            )
            await self.db.execute(
                sql_delete(CreatorCurationState).where(
                    CreatorCurationState.creator_id.in_(scope.creator_ids)
                )
            )
            await self.db.execute(
                sql_delete(Creator).where(Creator.id.in_(scope.creator_ids))
            )

        await request_search_projection(
            self.db,
            scope.affected_work_ids,
            deleted_creator_ids=scope.creator_ids if scope.entity_type == "creator" else (),
            deleted_repository_ids=scope.repository_ids,
            deleted_subscription_ids=(
                scope.subscription_ids
                if scope.entity_type in {"subscription", "creator"}
                else ()
            ),
            subscription_ids=(
                scope.subscription_ids if scope.entity_type == "repository" else ()
            ),
            creator_ids=(
                scope.creator_ids if scope.entity_type != "creator" else ()
            ),
        )
        await self.db.commit()

    @classmethod
    def _chunks(cls, values: set[UUID]):
        ordered = sorted(values, key=str)
        for index in range(0, len(ordered), cls.WORK_BATCH_SIZE):
            yield ordered[index:index + cls.WORK_BATCH_SIZE]


async def enqueue_permanent_deletion(
    entity_type: DeletionEntityType,
    entity_ids: list[UUID],
    *,
    delete_files: bool,
) -> dict:
    from app.services.operations import enqueue_admin_operation

    entity_ids = sorted(set(entity_ids), key=str)
    operation = await enqueue_admin_operation(
        lock_key="library:hierarchy-delete:active",
        operation_type="hierarchy-delete",
        title=f"Delete {entity_type}",
        entity="hierarchy-delete",
        func="app.jobs.admin_operations.run_hierarchy_delete_operation",
        options={
            "entity_type": entity_type,
            "entity_ids": [str(value) for value in entity_ids],
            "delete_files": delete_files,
        },
        job_timeout=7 * 24 * 60 * 60,
        queue_name="maintenance" if delete_files else "operations",
    )
    return {
        "status": "enqueued",
        "mode": "permanent",
        "entity_type": entity_type,
        "entity_ids": entity_ids,
        "delete_files": delete_files,
        "task_id": UUID(operation["job_id"]),
        "message": "Permanent deletion queued.",
    }
