"""Map global curation changes to the per-source-creator .gitllery repos they affect."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetSource, CurationChange, SourceCreator, Subscription,
    SubscriptionSource, WorkSource,
)
from app.services.library_sync import resolve_creator_directory


@dataclass
class RepoDescriptor:
    repository_id: str
    source: str
    source_creator_id: str | None
    creator_id: str | None
    creator_dir: str

    def key(self) -> str:
        return self.repository_id


class RepoResolver:
    """Resolve entities (work/creator/asset/repository) to RepoDescriptors.

    Create one per projection pass; lookups are cached on the instance.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self._repo_lookup: dict[tuple[str, str], SubscriptionSource] | None = None
        self._creator_by_subscription: dict[UUID, str | None] = {}
        self._creator_by_source_creator: dict[tuple[str, str], str | None] = {}
        self._change_repos_cache: dict[tuple[str, str], list[RepoDescriptor]] = {}
        self._work_sources_by_work: dict[str, list[WorkSource]] | None = None

    async def preload_work_sources(self, work_ids: list[str] | None = None) -> None:
        """Bulk-load WorkSource rows (one query) so per-change work resolution
        in bulk walks does zero extra DB round-trips.

        work_ids=None loads the whole library (project_pending — completeness
        is its job); a list scopes the load to those works (status tail path —
        O(tail), not O(library))."""
        if work_ids is not None and not work_ids:
            self._work_sources_by_work = {}
            return
        stmt = select(WorkSource)
        if work_ids is not None:
            valid: list[UUID] = []
            for w in work_ids:
                try:
                    valid.append(UUID(w))
                except (ValueError, TypeError):
                    continue
            if not valid:
                self._work_sources_by_work = {}
                return
            stmt = stmt.where(WorkSource.work_id.in_(valid))
        rows = await self.db.execute(stmt)
        grouped: dict[str, list[WorkSource]] = {}
        for ws in rows.scalars().all():
            grouped.setdefault(str(ws.work_id), []).append(ws)
        self._work_sources_by_work = grouped

    async def _ensure_repo_lookup(self) -> dict[tuple[str, str], SubscriptionSource]:
        if self._repo_lookup is None:
            rows = await self.db.execute(
                select(SubscriptionSource)
                .where(SubscriptionSource.source_creator_id.isnot(None))
                .order_by(SubscriptionSource.created_at)
            )
            lookup: dict[tuple[str, str], SubscriptionSource] = {}
            for repo in rows.scalars().all():
                if repo.source_creator_id:
                    lookup.setdefault((repo.source, repo.source_creator_id), repo)
            self._repo_lookup = lookup
        return self._repo_lookup

    async def _creator_for_subscription(self, subscription_id: UUID) -> str | None:
        if subscription_id not in self._creator_by_subscription:
            row = await self.db.execute(
                select(Subscription.creator_id).where(Subscription.id == subscription_id))
            cid = row.scalar_one_or_none()
            self._creator_by_subscription[subscription_id] = str(cid) if cid else None
        return self._creator_by_subscription[subscription_id]

    async def _creator_for_source_creator(self, source: str, scid: str) -> str | None:
        key = (source, scid)
        if key not in self._creator_by_source_creator:
            row = await self.db.execute(
                select(SourceCreator.creator_id).where(
                    SourceCreator.source == source,
                    SourceCreator.source_creator_id == scid))
            cid = row.scalar_one_or_none()
            self._creator_by_source_creator[key] = str(cid) if cid else None
        return self._creator_by_source_creator[key]

    async def _descriptor_for_work_source(self, ws: WorkSource) -> RepoDescriptor | None:
        if not ws.source_creator_id:
            return None
        creator_dir = resolve_creator_directory(ws.source, ws.raw_metadata or {}, ws.source_work_id)
        lookup = await self._ensure_repo_lookup()
        repo = lookup.get((ws.source, ws.source_creator_id))
        if repo:
            repository_id = str(repo.id)
            creator_id = await self._creator_for_subscription(repo.subscription_id)
        else:
            repository_id = f"{ws.source}:{ws.source_creator_id}"
            creator_id = await self._creator_for_source_creator(ws.source, ws.source_creator_id)
        return RepoDescriptor(repository_id=repository_id, source=ws.source,
                              source_creator_id=ws.source_creator_id,
                              creator_id=creator_id, creator_dir=creator_dir)

    async def _descriptors_for_creator(self, creator_id: UUID) -> list[RepoDescriptor]:
        rows = await self.db.execute(
            select(SourceCreator).where(SourceCreator.creator_id == creator_id))
        out: list[RepoDescriptor] = []
        for sc in rows.scalars().all():
            ws_row = await self.db.execute(
                select(WorkSource).where(
                    WorkSource.source == sc.source,
                    WorkSource.source_creator_id == sc.source_creator_id).limit(1))
            ws = ws_row.scalar_one_or_none()
            creator_dir = resolve_creator_directory(
                sc.source, ws.raw_metadata if ws else {},
                ws.source_work_id if ws else (sc.source_creator_id or ""))
            lookup = await self._ensure_repo_lookup()
            repo = lookup.get((sc.source, sc.source_creator_id))
            repository_id = str(repo.id) if repo else f"{sc.source}:{sc.source_creator_id}"
            out.append(RepoDescriptor(repository_id=repository_id, source=sc.source,
                                      source_creator_id=sc.source_creator_id,
                                      creator_id=str(creator_id), creator_dir=creator_dir))
        return out

    async def _descriptors_for_asset(self, asset_id: UUID) -> list[RepoDescriptor]:
        rows = await self.db.execute(
            select(WorkSource).join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .where(AssetSource.asset_id == asset_id))
        out: list[RepoDescriptor] = []
        for ws in rows.scalars().unique().all():
            desc = await self._descriptor_for_work_source(ws)
            if desc:
                out.append(desc)
        return out

    async def _descriptors_for_repository(self, repository_id: str) -> list[RepoDescriptor]:
        try:
            repo_uuid = UUID(repository_id)
        except (ValueError, TypeError):
            return []
        row = await self.db.execute(
            select(SubscriptionSource).where(SubscriptionSource.id == repo_uuid))
        repo = row.scalar_one_or_none()
        if not repo or not repo.source_creator_id:
            return []
        ws_row = await self.db.execute(
            select(WorkSource).where(
                WorkSource.source == repo.source,
                WorkSource.source_creator_id == repo.source_creator_id).limit(1))
        ws = ws_row.scalar_one_or_none()
        if ws is None:
            # No downloaded work for this subscription source → no on-disk
            # library directory to project into. Skip (e.g. reference-only or
            # not-yet-downloaded cross-platform subscription sources), otherwise
            # creator_dir falls back to "unknown" and unrelated sources collapse
            # into a single bogus {source}/unknown/.gitllery.
            return []
        creator_dir = resolve_creator_directory(
            repo.source, ws.raw_metadata, ws.source_work_id)
        creator_id = await self._creator_for_subscription(repo.subscription_id)
        return [RepoDescriptor(repository_id=str(repo.id), source=repo.source,
                               source_creator_id=repo.source_creator_id,
                               creator_id=creator_id, creator_dir=creator_dir)]

    async def _repos_for_change(self, change: CurationChange) -> list[RepoDescriptor]:
        # Cache per (subject_type, subject_id): an entity's repo membership does
        # not change within one projection/status pass, so repeated changes on the
        # same subject (trash→restore→favorite) resolve against the DB only once.
        key = (change.subject_type, change.subject_id)
        cached = self._change_repos_cache.get(key)
        if cached is not None:
            return cached
        result = await self._resolve_repos_for_change(change)
        self._change_repos_cache[key] = result
        return result

    async def _resolve_repos_for_change(self, change: CurationChange) -> list[RepoDescriptor]:
        st, sid = change.subject_type, change.subject_id
        if st == "work":
            if self._work_sources_by_work is not None:
                ws_list = self._work_sources_by_work.get(str(sid), [])
            else:
                rows = await self.db.execute(
                    select(WorkSource).where(WorkSource.work_id == UUID(sid)))
                ws_list = list(rows.scalars().all())
            out = []
            for ws in ws_list:
                desc = await self._descriptor_for_work_source(ws)
                if desc:
                    out.append(desc)
            return out
        if st == "asset":
            return await self._descriptors_for_asset(UUID(sid))
        if st == "creator":
            return await self._descriptors_for_creator(UUID(sid))
        if st == "repository":
            return await self._descriptors_for_repository(sid)
        return []

    async def slice_changes(
        self, changes: list[CurationChange]
    ) -> dict[str, tuple[RepoDescriptor, list[CurationChange]]]:
        result: dict[str, tuple[RepoDescriptor, list[CurationChange]]] = {}
        for change in changes:
            for desc in await self._repos_for_change(change):
                if desc.key() not in result:
                    result[desc.key()] = (desc, [])
                result[desc.key()][1].append(change)
        return result

    async def all_repositories(self) -> list[RepoDescriptor]:
        rows = await self.db.execute(
            select(WorkSource.source, WorkSource.source_creator_id)
            .where(WorkSource.source_creator_id.isnot(None)).distinct())
        out: list[RepoDescriptor] = []
        seen: set[str] = set()
        for source, scid in rows.all():
            ws_row = await self.db.execute(
                select(WorkSource).where(
                    WorkSource.source == source,
                    WorkSource.source_creator_id == scid).limit(1))
            ws = ws_row.scalar_one_or_none()
            if not ws:
                continue
            desc = await self._descriptor_for_work_source(ws)
            if desc and desc.key() not in seen:
                seen.add(desc.key())
                out.append(desc)
        return out
