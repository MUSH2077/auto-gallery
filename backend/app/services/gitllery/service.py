"""GitlleryService — project the authoritative curation DAG onto disk.

PostgreSQL remains authoritative; disk projection is best-effort and
idempotent. A bulk catch-up also fences the durable intents it has covered.
"""
from __future__ import annotations

import asyncio
from itertools import islice
import logging
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    CurationChange,
    CurationCommit,
    GitlleryProjectionOutbox,
    GitlleryProjectionTarget,
    GitlleryBuild,
    GitlleryRepositoryState,
)
from app.services.gitllery.objects import hash_payload
from app.services.gitllery.repo import GitlleryRepo, SCHEMA_VERSION
from app.services.gitllery.slicing import RepoDescriptor, RepoResolver
from app.services.heavy_io import LocalHeavyIOLock
from app.services.locks import redis_lock
from gitllery_format import SegmentRepository

logger = logging.getLogger(__name__)


def gitllery_projection_lock() -> LocalHeavyIOLock:
    """Process-lifetime ordering fence shared by bulk and outbox writers."""

    return LocalHeavyIOLock(
        Path(settings.library_root) / ".gitllery-projection.lock"
    )


def _change_payload(change: CurationChange) -> dict:
    return {
        "subject_type": change.subject_type,
        "subject_id": change.subject_id,
        "action": change.action,
        "diff": change.diff or {},
        "impact": change.impact or {},
    }


class GitlleryService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.last_projection_high_water: tuple[datetime, UUID] | None = None
        self.continuation: dict | None = None
        self.successor_delay_seconds = 2.0
        self._validated_bulk_watermarks: dict[UUID, datetime] = {}

    async def _changes_for_commit(self, commit_id: UUID) -> list[CurationChange]:
        rows = await self.db.execute(
            select(CurationChange).where(CurationChange.commit_id == commit_id)
            .order_by(
                CurationChange.sequence.asc().nulls_last(),
                CurationChange.created_at,
                CurationChange.id,
            ))
        return list(rows.scalars().all())

    def _repo_for(self, desc: RepoDescriptor) -> GitlleryRepo:
        return GitlleryRepo(Path(settings.library_root), desc.source, desc.creator_dir)

    def _segment_repo_for(self, desc: RepoDescriptor) -> SegmentRepository:
        creator_root = (
            Path(settings.library_root) / desc.source / desc.creator_dir
        ).resolve()
        creator_root.relative_to(Path(settings.library_root).resolve())
        if settings.gitllery_projection_mode == "active":
            dirname = ".gitllery"
        else:
            generation = "".join(
                character
                for character in settings.gitllery_build_generation
                if character.isalnum() or character in {"-", "_"}
            ) or "segment-r1"
            dirname = f".gitllery.build-{generation}"
        repository = SegmentRepository(creator_root / dirname)
        repository.root.relative_to(Path(settings.library_root).resolve())
        return repository

    @staticmethod
    def _segment_commit_payload(
        commit: CurationCommit,
        changes: list[CurationChange],
        *,
        repository_parent_commit_id: str | None,
    ) -> dict:
        return {
            "commit_id": str(commit.id),
            # Each portable repository has its own complete chain. Keep the
            # authoritative global parent separately for audit/debugging.
            "parent_commit_id": repository_parent_commit_id,
            "authoritative_parent_commit_id": (
                str(commit.parent_commit_id) if commit.parent_commit_id else None
            ),
            "actor_type": commit.actor_type,
            "actor_id": commit.actor_id,
            "message": commit.message,
            "trigger": commit.trigger,
            "dedupe_key": commit.dedupe_key,
            "reverts_commit_id": (
                str(commit.reverts_commit_id) if commit.reverts_commit_id else None
            ),
            "occurred_at": (
                commit.occurred_at.isoformat() if commit.occurred_at else None
            ),
            "created_at": commit.created_at.isoformat(),
            "stats": commit.stats or {},
            "metadata": commit.extra_metadata or {},
            "changes": [
                {
                    "change_id": str(change.id),
                    "subject_type": change.subject_type,
                    "subject_id": change.subject_id,
                    "action": change.action,
                    "sequence": change.sequence,
                    "before_state": change.before_state,
                    "after_state": change.after_state,
                    "diff": change.diff or {},
                    "impact": change.impact or {},
                }
                for change in changes
            ],
        }

    def _project_commit_to_segment_repo(
        self,
        desc: RepoDescriptor,
        commit: CurationCommit,
        changes: list[CurationChange],
    ) -> bool:
        repo = self._segment_repo_for(desc)
        if settings.gitllery_projection_mode == "active":
            from app.services.gitllery.builds import assert_active_repository_ready

            assert_active_repository_ready(repo)
        with repo.projection_lock():
            repo.initialise(
                repository_id=desc.repository_id,
                source=desc.source,
                creator_dir=desc.creator_dir,
                generation=settings.gitllery_build_generation,
            )
            manifest = repo.read_manifest()
            commit_id = str(commit.id)
            if str(manifest.get("last_complete_commit_id")) == commit_id:
                return False
            payload = self._segment_commit_payload(
                commit,
                changes,
                repository_parent_commit_id=manifest.get(
                    "last_complete_commit_id"
                ),
            )
            repo.append([payload])
            return True

    def _ensure_init(self, repo: GitlleryRepo, desc: RepoDescriptor) -> None:
        if repo.exists():
            return
        repo.init(
            {
                "schema_version": SCHEMA_VERSION,
                "repository_id": desc.repository_id,
                "source": desc.source,
                "creator_id": desc.creator_id,
                "source_creator_id": desc.source_creator_id,
                "creator_dir": desc.creator_dir,
                "object_hash": "sha256",
                "compression": "zlib",
                "projection_layout": "sharded-v2",
                "entity_shard": "sha256-prefix-2",
            },
            f"{desc.source} / {desc.creator_dir} curation history",
        )

    def _apply_commit_to_repo(
        self, repo: GitlleryRepo, desc: RepoDescriptor,
        commit: CurationCommit, changes: list[CurationChange],
        *,
        projected_ids: set[str] | None = None,
        ordered_outbox: bool = False,
    ) -> str | None:
        """Append one bounded v2 projection under the repository file lock."""

        self._ensure_init(repo, desc)
        with repo.projection_lock():
            return self._apply_commit_to_repo_locked(
                repo,
                commit,
                changes,
                projected_ids=projected_ids,
                ordered_outbox=ordered_outbox,
            )

    def _apply_commit_to_repo_locked(
        self,
        repo: GitlleryRepo,
        commit: CurationCommit,
        changes: list[CurationChange],
        *,
        projected_ids: set[str] | None,
        ordered_outbox: bool,
    ) -> str | None:
        commit_id = str(commit.id)
        head = repo.head_commit()
        head_object: dict = {}
        if head:
            try:
                head_object = repo.objects.read(head)
            except (OSError, ValueError, zlib.error) as exc:
                raise ValueError("cannot resolve Gitllery HEAD") from exc
            if hash_payload(head_object) != head:
                raise ValueError("Gitllery HEAD failed content verification")

        # The only ambiguous crash window is HEAD advanced / root manifest not
        # yet replaced. The commit embeds the complete small manifest, so retry
        # repairs that watermark in O(1) and never appends a duplicate.
        if head_object.get("db_commit_id") == commit_id:
            repo.recover_projection_manifest(head, head_object)
            return None

        manifest = repo.projection_manifest_for_update()
        already_projected = (
            commit_id in projected_ids
            if projected_ids is not None
            else manifest.get("last_db_commit_id") == commit_id
        )
        if already_projected:
            return None

        affected_shards: dict[str, dict] = {}
        changed_entries: dict[str, str] = {}
        payloads: list[dict] = []
        base_is_legacy = manifest.get("base") is not None
        for change in changes:
            entity_key = f"{change.subject_type}/{change.subject_id}"
            prefix = repo.shard_for_entity(entity_key)
            shard = affected_shards.get(prefix)
            if shard is None:
                shard = repo.read_projection_shard(prefix, manifest)
                affected_shards[prefix] = shard
            entities = shard.setdefault("entities", {})
            entries = shard.setdefault("entries", {})
            after = change.after_state
            if after is None:
                changed_entries.pop(entity_key, None)
                if base_is_legacy:
                    # An overlay needs a tombstone to hide its immutable v1
                    # base. A fully migrated v2 shard can simply remove the key.
                    entities[entity_key] = None
                    entries[entity_key] = None
                else:
                    entities.pop(entity_key, None)
                    entries.pop(entity_key, None)
                continue
            blob = {
                "type": "blob",
                "subject_type": change.subject_type,
                "subject_id": change.subject_id,
                "state": after,
            }
            blob_hash = hash_payload(blob)
            payloads.append(blob)
            entities[entity_key] = after
            entries[entity_key] = blob_hash
            changed_entries[entity_key] = blob_hash

        shards = dict(manifest.get("shards") or {})
        for prefix, shard in affected_shards.items():
            entities = dict(shard.get("entities") or {})
            entries = dict(shard.get("entries") or {})
            if not entities and not entries and not base_is_legacy:
                shards.pop(prefix, None)
                continue
            shard_payload = {
                "type": "tree-shard-v2",
                "prefix": prefix,
                "entities": entities,
                "entries": entries,
            }
            payloads.append(shard_payload)
            shards[prefix] = hash_payload(shard_payload)

        tree_payload = {
            "type": "tree-v2",
            "schema_version": SCHEMA_VERSION,
            "base_tree": (
                (manifest.get("base") or {}).get("tree")
                if base_is_legacy
                else None
            ),
            "shards": shards,
            # Compatibility delta: rebuild only asks each historical commit's
            # tree for entities changed by that commit.
            "entries": changed_entries,
        }
        tree_hash = hash_payload(tree_payload)
        commit_created_at = (
            getattr(commit, "created_at", None)
            or commit.occurred_at
            or datetime.now(timezone.utc)
        )
        projection = {
            "schema_version": SCHEMA_VERSION,
            "generation": int(manifest.get("generation") or 0) + 1,
            "tree": tree_hash,
            "last_db_commit_id": commit_id,
            "last_db_created_at": commit_created_at.isoformat(),
            "base": manifest.get("base"),
            "shards": shards,
        }
        commit_payload = {
            "type": "commit",
            "tree": tree_hash,
            "parent": head,
            "db_commit_id": commit_id,
            "actor_type": commit.actor_type,
            "actor_id": commit.actor_id,
            "message": commit.message,
            "trigger": commit.trigger,
            "dedupe_key": commit.dedupe_key,
            "reverts": (
                str(commit.reverts_commit_id)
                if commit.reverts_commit_id
                else None
            ),
            "occurred_at": (
                commit.occurred_at.isoformat()
                if commit.occurred_at
                else None
            ),
            "stats": commit.stats or {},
            "changes": [_change_payload(change) for change in changes],
            "projection": projection,
        }
        commit_hash = hash_payload(commit_payload)

        # Bulk blobs and affected shards share one fsync. Keep the small tree
        # and commit loose so routine HEAD/tree probes never inflate a pack
        # containing media-sized entity states or shard snapshots. The total
        # remains bounded well below 20 fsyncs per projection.
        repo.objects.write_many(payloads)
        repo.objects.write(tree_payload)
        repo.objects.write(commit_payload)

        # Object publication precedes the ref; the small manifest follows it.
        # This ordering makes every crash state either unreachable garbage or
        # recoverable from the new HEAD commit.
        actor = commit.actor_id or commit.actor_type or "system"
        repo.set_head(commit_hash, actor=actor, message=commit.message)
        repo.write_projection_manifest({**projection, "head": commit_hash})
        return commit_hash

    _BULK_BATCH = 25

    async def _commit_batches(
        self, high_water: tuple[datetime, UUID], after: tuple[datetime, UUID] | None = None,
    ):
        """Yield ordered commits in keyset-paged batches of _BULK_BATCH.

        Bulk walks (reconcile/backfill jobs) must never materialize the whole
        commit history as ORM objects — on a large library that is hundreds of
        MB and the reason these walks now run in a queue worker, not backend.
        """
        last = after
        while True:
            stmt = (
                select(CurationCommit)
                .where(
                    tuple_(CurationCommit.created_at, CurationCommit.id)
                    <= tuple_(*high_water)
                )
                .order_by(
                    CurationCommit.created_at,
                    CurationCommit.id,
                )
                .limit(self._BULK_BATCH)
            )
            if last is not None:
                stmt = stmt.where(
                    tuple_(CurationCommit.created_at, CurationCommit.id)
                    > tuple_(*last))
            batch = list((await self.db.execute(stmt)).scalars().all())
            if not batch:
                return
            yield batch
            last = (batch[-1].created_at, batch[-1].id)

    async def project_pending(
        self,
        repository_id: str | None = None,
        *,
        resource_owner: str | None = None,
        continuation: dict | None = None,
    ) -> dict[str, int]:
        """Project one ordered slice, exposing its durable successor cursor.

        Admin dispatch persists ``continuation`` only after this prefix and its
        outbox acknowledgments commit. A crash before handoff replays at most
        this slice; repository append checks make that replay idempotent.
        """
        from contextlib import aclosing
        from contextlib import AsyncExitStack
        from app.services.heavy_io import adaptive_resource_slice
        from app.services.operations import fence_current_admin_operation_transaction

        state = dict(continuation or {})
        self.continuation = None
        if state.get("high_water"):
            raw = state["high_water"]
            high_water = (datetime.fromisoformat(raw[0]), UUID(raw[1]))
        else:
            row = (await self.db.execute(
                select(CurationCommit.created_at, CurationCommit.id)
                .order_by(CurationCommit.created_at.desc(), CurationCommit.id.desc())
                .limit(1)
            )).first()
            if row is None:
                self.last_projection_high_water = None
                await self.db.commit()
                return {}
            high_water = (row[0], row[1])
            state["high_water"] = [high_water[0].isoformat(), str(high_water[1])]
        self.last_projection_high_water = high_water
        after = state.get("after")
        after = (datetime.fromisoformat(after[0]), UUID(after[1])) if after else None
        counts: dict[str, int] = {}
        resolver = RepoResolver(self.db)
        async with aclosing(self._commit_batches(high_water, after)) as batches:
            batch = await anext(batches, [])
        if not batch:
            await self.db.commit()
            return counts
        changes = await self._changes_for_commits([c.id for c in batch])
        await resolver.preload_work_sources(work_ids=sorted({
            ch.subject_id for group in changes.values() for ch in group
            if ch.subject_type == "work"
        }))
        prepared = [(commit, await resolver.slice_changes(changes.get(commit.id, [])))
                    for commit in batch]
        await self.db.commit()
        cooldown: dict[str, float] = {}
        self.continuation = state
        async with adaptive_resource_slice(
            "git_projection", resource_owner or f"gitllery-sync:{repository_id or 'all'}",
            max_work_units=len(prepared), max_slice_seconds=20.0,
            wait_for_capacity=False, cooldown_result=cooldown,
        ) as limits:
            if limits is None:
                return counts
            started = monotonic()
            selected = prepared[:max(1, min(int(limits.work_units), len(prepared)))]
            for commit, sliced in selected:
                async with AsyncExitStack() as locks:
                    # Acquire nonwaiting filesystem publication locks before
                    # opening the fenced business transaction.
                    for rid in sorted(sliced):
                        acquired = await locks.enter_async_context(redis_lock(f"gitllery:{rid}", ttl_seconds=300))
                        if not acquired:
                            raise RuntimeError(f"gitllery repository {rid} is busy")
                    intent = (await self.db.execute(select(GitlleryProjectionOutbox)
                        .where(GitlleryProjectionOutbox.commit_id == commit.id)
                        .with_for_update())).scalar_one_or_none()
                    # Domain rows precede TaskRun in the global lock order.
                    # Keep this fence until disk publication and ack commit.
                    await fence_current_admin_operation_transaction(self.db)
                    for rid, (desc, repo_changes) in sliced.items():
                        repo = self._repo_for(desc)
                        self._ensure_init(repo, desc)
                        already_present = await self._assert_append_order(
                            repo, commit, completed_intent=intent is not None and intent.state == "complete")
                        if already_present:
                            continue
                        new_hash = self._apply_commit_to_repo(
                            repo, desc, commit, repo_changes, ordered_outbox=True,
                        )
                        if new_hash is not None:
                            counts[rid] = counts.get(rid, 0) + 1
                    await self._complete_bulk_outbox([commit.id])
                state["after"] = [commit.created_at.isoformat(), str(commit.id)]
                if limits.slice_seconds is not None and monotonic() - started >= limits.slice_seconds:
                    break
            logger.info("Git history slice complete", extra={"resource_slice": {
                "elapsed_seconds": monotonic() - started, "limit_seconds": limits.slice_seconds}})
        self.successor_delay_seconds = max(2.0, cooldown.get("seconds", 0.0))
        if counts:
            _invalidate_status_cache()
        return counts

    async def _complete_bulk_outbox(self, commit_ids: list[UUID]) -> None:
        """Fence intents covered by a successful library-wide bulk prefix."""

        if not commit_ids:
            return
        from app.services.operations import fence_current_admin_operation_transaction

        await self.db.execute(
            update(GitlleryProjectionOutbox)
            .where(
                GitlleryProjectionOutbox.commit_id.in_(commit_ids),
                GitlleryProjectionOutbox.state != "complete",
            )
            .values(
                state="complete",
                completed_at=datetime.now(timezone.utc),
                lease_expires_at=None,
                last_error=None,
                projection_stats={"mode": "bulk_sync"},
            )
        )
        await fence_current_admin_operation_transaction(self.db)
        await self.db.commit()

    async def backfill(
        self,
        *,
        resource_owner: str | None = None,
        continuation: dict | None = None,
    ) -> dict[str, int]:
        """Initialize a bounded repository prefix, then continue history."""
        from app.services.heavy_io import adaptive_resource_slice
        from app.services.operations import fence_current_admin_operation_transaction

        state = dict(continuation or {})
        if state.get("phase") == "project":
            return await self.project_pending(resource_owner=resource_owner, continuation=state)
        descriptors = await RepoResolver(self.db).repository_page(
            after=tuple(state["repository_cursor"]) if state.get("repository_cursor") else None,
            limit=self._BULK_BATCH,
        )
        await self.db.commit()
        self.continuation = state
        if not descriptors:
            state["phase"] = "project"
            return {}
        cooldown: dict[str, float] = {}
        async with adaptive_resource_slice(
            "git_projection", resource_owner or "gitllery-backfill",
            max_work_units=min(len(descriptors), self._BULK_BATCH), max_slice_seconds=20.0,
            wait_for_capacity=False, cooldown_result=cooldown,
        ) as limits:
            if limits is None:
                return {}
            started = monotonic()
            for desc in descriptors[:max(1, int(limits.work_units))]:
                async with redis_lock(f"gitllery:{desc.key()}", ttl_seconds=300) as acquired:
                    if not acquired:
                        raise RuntimeError(f"gitllery repository {desc.key()} is busy")
                    await fence_current_admin_operation_transaction(self.db)
                    self._ensure_init(self._repo_for(desc), desc)
                    await self.db.commit()
                state["repository_after"] = desc.key()
                state["repository_cursor"] = [desc.source, desc.source_creator_id]
                if limits.slice_seconds is not None and monotonic() - started >= limits.slice_seconds:
                    break
        self.successor_delay_seconds = max(2.0, cooldown.get("seconds", 0.0))
        return {}

    async def migrate_repository_v2(self, repository_id: str) -> dict:
        """Explicit maintenance migration; never called by project_commit."""

        resolver = RepoResolver(self.db)
        desc = next(
            (
                candidate
                for candidate in await resolver.all_repositories()
                if candidate.key() == repository_id
            ),
            None,
        )
        if desc is None:
            raise HTTPException(status_code=404, detail="repository not found")
        repo = self._repo_for(desc)
        self._ensure_init(repo, desc)
        async with redis_lock(
            f"gitllery:{repository_id}",
            ttl_seconds=1800,
        ) as acquired:
            if not acquired:
                raise RuntimeError(
                    f"gitllery repository {repository_id} is busy"
                )
            result = await asyncio.to_thread(repo.migrate_v1_to_v2)
        if result.get("migrated"):
            _invalidate_status_cache()
        return {"repository_id": repository_id, **result}

    async def project_commit(
        self,
        commit_id: UUID,
        *,
        ordered_outbox: bool = False,
    ) -> list[str]:
        commit = await self.db.get(CurationCommit, commit_id)
        if commit is None:
            return []
        changes = await self._changes_for_commit(commit_id)
        sliced = await RepoResolver(self.db).slice_changes(changes)
        affected: list[str] = []
        for repository_id, (desc, repo_changes) in sliced.items():
            async with redis_lock(f"gitllery:{repository_id}", ttl_seconds=60) as acquired:
                if not acquired:
                    logger.warning("gitllery: could not lock repo %s; skipping (catch-up will retry)",
                                   repository_id)
                    if ordered_outbox:
                        raise RuntimeError(
                            f"gitllery repository {repository_id} is busy"
                        )
                    continue
                mode = settings.gitllery_projection_mode.strip().lower()
                if mode not in {"shadow", "active"}:
                    raise RuntimeError(
                        f"unsupported Gitllery projection mode: {mode}"
                    )
                projected = await asyncio.to_thread(
                    self._project_commit_to_segment_repo,
                    desc,
                    commit,
                    repo_changes,
                )
                if projected:
                    affected.append(repository_id)
        if affected:
            _invalidate_status_cache()
        return affected

    async def _assert_append_order(
        self,
        repo: GitlleryRepo,
        commit: CurationCommit,
        *,
        completed_intent: bool = False,
    ) -> bool:
        """Refuse a historical live-HEAD replay; gaps require staged rebuild."""

        if not repo.exists():
            return False
        manifest = repo.projection_manifest_for_update()
        watermark = manifest.get("last_db_commit_id")
        if not watermark or str(watermark) == str(commit.id):
            return False
        watermark_created_at = manifest.get("last_db_created_at")
        watermark_id: UUID
        try:
            watermark_id = UUID(str(watermark))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Gitllery manifest has an invalid DB watermark") from exc
        if watermark_created_at:
            watermark_ts = datetime.fromisoformat(str(watermark_created_at))
        else:
            watermark_commit = await self.db.get(CurationCommit, watermark_id)
            if watermark_commit is None:
                raise RuntimeError(
                    "Gitllery manifest watermark is absent from PostgreSQL"
                )
            watermark_ts = watermark_commit.created_at
        current_ts = commit.created_at
        if current_ts.tzinfo is None:
            current_ts = current_ts.replace(tzinfo=timezone.utc)
        if watermark_ts.tzinfo is None:
            watermark_ts = watermark_ts.replace(tzinfo=timezone.utc)
        if (current_ts, commit.id) < (watermark_ts, watermark_id):
            if completed_intent:
                # A completed intent plus a valid durable watermark proves
                # this prefix. Deep parent scans belong to incomplete/gap
                # recovery, not every commit of an ordinary reconciliation.
                if watermark_id not in self._validated_bulk_watermarks:
                    stored_ts = (await self.db.execute(select(CurationCommit.created_at)
                        .where(CurationCommit.id == watermark_id))).scalar_one_or_none()
                    if stored_ts is None:
                        raise RuntimeError("Gitllery manifest watermark is absent from PostgreSQL")
                    self._validated_bulk_watermarks[watermark_id] = stored_ts
                if self._validated_bulk_watermarks[watermark_id] != watermark_ts:
                    raise RuntimeError("Gitllery manifest watermark timestamp does not match PostgreSQL")
                return True
            if await asyncio.to_thread(
                repo.has_projected_db_commit_id,
                str(commit.id),
            ):
                return True
            raise RuntimeError(
                "Gitllery pre-watermark projection gap requires a staged rebuild"
            )
        return False

    async def _tail_commits(self, checkpoint: tuple[datetime, str]) -> list[CurationCommit]:
        """Commits strictly after the checkpoint position (created_at, id)."""
        from sqlalchemy import and_, or_
        ts, cid = checkpoint
        rows = await self.db.execute(
            select(CurationCommit).where(
                or_(CurationCommit.created_at > ts,
                    and_(CurationCommit.created_at == ts, CurationCommit.id > UUID(cid))))
            .order_by(CurationCommit.created_at, CurationCommit.id))
        return list(rows.scalars().all())

    async def _changes_for_commits(self, commit_ids: list[UUID]) -> dict[UUID, list[CurationChange]]:
        if not commit_ids:
            return {}
        rows = await self.db.execute(
            select(CurationChange).where(CurationChange.commit_id.in_(commit_ids))
            .order_by(CurationChange.created_at, CurationChange.id))
        grouped: dict[UUID, list[CurationChange]] = {}
        for ch in rows.scalars().all():
            grouped.setdefault(ch.commit_id, []).append(ch)
        return grouped

    @staticmethod
    def _projected_tail_ids(repo: GitlleryRepo, tail_ids: set[str]) -> set[str]:
        """Walk from HEAD, stopping at the first db_commit_id outside the tail
        set — exact under the checkpoint invariant: everything ≤ P is already
        in the chains, post-P projections are all tail ids, and chains are
        append-only, so the HEAD side is a contiguous run of tail ids."""
        seen: set[str] = set()
        cur = repo.head_commit()
        guard = 0
        while cur and guard < 1_000_000:
            guard += 1
            try:
                obj = repo.objects.read(cur)
            except (OSError, ValueError, zlib.error):
                break
            db_id = obj.get("db_commit_id")
            if db_id:
                if db_id not in tail_ids:
                    break
                seen.add(db_id)
            cur = obj.get("parent")
        return seen

    async def _pending_count_tail(
        self, repository_id: str | None, tail: list[CurationCommit],
    ) -> tuple[dict[str, int], dict[str, RepoDescriptor], tuple | None]:
        """Tail-only variant of _pending_count_by_repo — O(tail), not O(history)."""
        resolver = RepoResolver(self.db)
        changes_by_commit = await self._changes_for_commits([c.id for c in tail])
        work_ids = sorted({ch.subject_id for chs in changes_by_commit.values()
                           for ch in chs if ch.subject_type == "work"})
        await resolver.preload_work_sources(work_ids=work_ids)
        tail_ids = {str(c.id) for c in tail}
        projected_cache: dict[str, set[str]] = {}
        repo_desc: dict[str, RepoDescriptor] = {}
        pending: dict[str, int] = {}
        for commit in tail:
            sliced = await resolver.slice_changes(changes_by_commit.get(commit.id, []))
            for rid, (desc, _chs) in sliced.items():
                if repository_id is not None and rid != repository_id:
                    continue
                repo_desc[rid] = desc
                if rid not in projected_cache:
                    repo = self._repo_for(desc)
                    projected_cache[rid] = (
                        await asyncio.to_thread(self._projected_tail_ids, repo, tail_ids)
                        if repo.exists() else set())
                if str(commit.id) not in projected_cache[rid]:
                    pending[rid] = pending.get(rid, 0) + 1
        for rid in repo_desc:
            pending.setdefault(rid, 0)
        max_pos = (tail[-1].created_at, tail[-1].id) if tail else None
        return pending, repo_desc, max_pos

    def _integrity_probe(self, repo: GitlleryRepo) -> bool:
        """Light-mode check: HEAD resolves and its commit object loads.

        The deep three-level verify (commit → tree → every blob re-hashed) costs
        O(entities) zlib inflations per repo; the probe is one object read."""
        head = repo.head_commit()
        if head is None:
            return True
        try:
            return repo.objects.read(head).get("type") == "commit"
        except (OSError, ValueError, zlib.error):
            return False

    def _integrity_ok(self, repo: GitlleryRepo) -> bool:
        head = repo.head_commit()
        if head is None:
            return True
        if not repo.objects.verify(head):
            return False
        commit_obj = repo.objects.read(head)
        tree_hash = commit_obj.get("tree")
        if not tree_hash or not repo.objects.verify(tree_hash):
            return False
        tree = repo.objects.read(tree_hash)
        for blob_hash in (tree.get("entries") or {}).values():
            if blob_hash and not repo.objects.verify(blob_hash):
                return False
        if tree.get("type") == "tree-v2":
            base_tree = tree.get("base_tree")
            if base_tree:
                if not repo.objects.verify(base_tree):
                    return False
                for blob_hash in (
                    repo.objects.read(base_tree).get("entries") or {}
                ).values():
                    if blob_hash and not repo.objects.verify(blob_hash):
                        return False
            for shard_hash in (tree.get("shards") or {}).values():
                if not repo.objects.verify(shard_hash):
                    return False
                shard = repo.objects.read(shard_hash)
                if shard.get("type") != "tree-shard-v2":
                    return False
                for blob_hash in (shard.get("entries") or {}).values():
                    if blob_hash and not repo.objects.verify(blob_hash):
                        return False
        return True

    async def _live_state(self, subject_type: str, subject_id: str) -> dict | None:
        from app.models import AssetStorageState, CreatorCurationState, WorkCurationState
        try:
            sid = UUID(subject_id)
        except (ValueError, TypeError):
            return None
        if subject_type == "work":
            row = await self.db.execute(
                select(WorkCurationState).where(WorkCurationState.work_id == sid))
            st = row.scalar_one_or_none()
            return {"visibility": st.visibility if st else "visible"}
        if subject_type == "creator":
            row = await self.db.execute(
                select(CreatorCurationState).where(CreatorCurationState.creator_id == sid))
            st = row.scalar_one_or_none()
            return {"visibility": st.visibility if st else "visible"}
        if subject_type == "asset":
            row = await self.db.execute(
                select(AssetStorageState).where(AssetStorageState.asset_id == sid))
            st = row.scalar_one_or_none()
            return {"storage_state": st.storage_state if st else "available"}
        return None

    async def _drift_keys(self, repo: GitlleryRepo) -> list[str]:
        index_entities = repo.read_index().get("entities", {}) if repo.exists() else {}
        drift: list[str] = []
        for entity_key, disk_state in index_entities.items():
            subject_type, _, subject_id = entity_key.partition("/")
            live = await self._live_state(subject_type, subject_id)
            if live is None:
                continue
            for field in ("visibility", "storage_state"):
                if field in live and disk_state.get(field) != live.get(field):
                    drift.append(entity_key)
                    break
        return drift

    async def status(self, repository_id: str | None = None, deep: bool = False) -> dict:
        mode = settings.gitllery_projection_mode.strip().lower()
        if mode in {"shadow", "active"}:
            return await self._segment_status(repository_id, deep=deep)
        if repository_id is not None:
            known = {d.key() for d in await RepoResolver(self.db).all_repositories()}
            if repository_id not in known:
                raise HTTPException(status_code=404, detail="repository not found")
        from app.services.cache import TTL, cache_get, cache_key, cache_set
        key = cache_key("gitllery:status", repository_id=repository_id, deep=deep)
        cached = cache_get(key)
        if cached is not None:
            return cached
        # Checkpoint fast path: only look at commits after P (verified-projected
        # position). There is deliberately NO full-history fallback here — that
        # walk loaded every commit/change/work_source into the backend process
        # (an OOM vector on a large library). When the checkpoint is absent
        # (fresh redis, post-rebuild), pending counts are unknown and the caller
        # must run the queued reconcile job, which recomputes ground truth in a
        # worker and re-establishes the checkpoint.
        checkpoint = _read_checkpoint()
        needs_reconcile = checkpoint is None
        max_pos: tuple | None = None
        if checkpoint is not None:
            tail = await self._tail_commits(checkpoint)
            pending, repo_desc, max_pos = await self._pending_count_tail(repository_id, tail)
        else:
            pending, repo_desc = {}, {}
        # Include repos that exist on disk / in DB even with zero history.
        for desc in await RepoResolver(self.db).all_repositories():
            if repository_id is None or desc.key() == repository_id:
                repo_desc.setdefault(desc.key(), desc)
        repos_out = []
        missing = 0
        behind_total = 0
        for rid, desc in repo_desc.items():
            repo = self._repo_for(desc)
            exists = repo.exists()
            if not exists:
                missing += 1
            behind = pending.get(rid, 0)
            behind_total += behind
            integrity = (
                await asyncio.to_thread(self._integrity_ok if deep else self._integrity_probe, repo)
                if exists else True)
            drift = (await self._drift_keys(repo)) if (deep and exists) else []
            repos_out.append({
                "repository_id": rid,
                "source": desc.source,
                "creator_dir": desc.creator_dir,
                "exists": exists,
                "behind": behind,
                "object_integrity_ok": integrity,
                "drift": drift,
                "clean": exists and behind == 0 and integrity and not drift and not needs_reconcile,
            })
        # Only a LIBRARY-WIDE clean result verifies the invariant for every
        # repo — per-repo status must never advance P.
        if repository_id is None and not needs_reconcile and behind_total == 0 and max_pos is not None:
            _advance_checkpoint(max_pos[0], max_pos[1])
        result = {"repositories": repos_out, "missing_repos": missing,
                  "behind_total": behind_total, "deep": deep,
                  "needs_reconcile": needs_reconcile}
        cache_set(key, result, TTL["gitllery:status"])
        return result

    async def _segment_status(
        self,
        repository_id: str | None,
        *,
        deep: bool,
    ) -> dict:
        if deep and repository_id is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "gitllery_async_verify_required",
                    "message": (
                        "Library-wide Gitllery verification is asynchronous; "
                        "use POST /gitllery/verify."
                    ),
                },
            )

        from app.services.cache import TTL, cache_get, cache_key, cache_set

        key = cache_key(
            "gitllery:status",
            repository_id=repository_id,
            deep=deep,
            format="segment-v1-db",
        )
        if not deep:
            cached = cache_get(key)
            if cached is not None:
                return cached

        descriptors = {
            descriptor.key(): descriptor
            for descriptor in await RepoResolver(self.db).all_repositories()
            if repository_id is None or descriptor.key() == repository_id
        }
        if repository_id is not None and repository_id not in descriptors:
            raise HTTPException(status_code=404, detail="repository not found")
        states_stmt = select(GitlleryRepositoryState)
        if repository_id is not None:
            states_stmt = states_stmt.where(
                GitlleryRepositoryState.repository_key == repository_id
            )
        states = {
            state.repository_key: state
            for state in (await self.db.execute(states_stmt)).scalars()
        }
        pending_stmt = (
            select(
                GitlleryProjectionTarget.repository_key,
                func.count(GitlleryProjectionTarget.id),
            )
            .where(GitlleryProjectionTarget.state != "complete")
            .group_by(GitlleryProjectionTarget.repository_key)
        )
        if repository_id is not None:
            pending_stmt = pending_stmt.where(
                GitlleryProjectionTarget.repository_key == repository_id
            )
        pending = {
            key: int(count)
            for key, count in (await self.db.execute(pending_stmt)).all()
        }

        has_targets = exists(
            select(GitlleryProjectionTarget.id).where(
                GitlleryProjectionTarget.intent_id == GitlleryProjectionOutbox.id
            )
        )
        unplanned_intents = int(
            (
                await self.db.execute(
                    select(func.count(GitlleryProjectionOutbox.id)).where(
                        GitlleryProjectionOutbox.state != "complete",
                        ~has_targets,
                    )
                )
            ).scalar_one()
            or 0
        )
        current_states = {
            key: state
            for key, state in states.items()
            if state.format_id == "gitllery-segment"
            and int(state.format_revision or 0) == 1
        }
        segment_repositories = len(current_states)
        legacy_repositories = max(0, len(descriptors) - segment_repositories)
        last_verified_at = max(
            (
                state.last_verified_at
                for state in current_states.values()
                if state.last_verified_at is not None
            ),
            default=None,
        )
        latest_operation = (
            await self.db.execute(
                select(GitlleryBuild)
                .order_by(GitlleryBuild.created_at.desc(), GitlleryBuild.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        latest_build = (
            await self.db.execute(
                select(GitlleryBuild)
                .where(GitlleryBuild.kind == "build")
                .order_by(GitlleryBuild.created_at.desc(), GitlleryBuild.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_operation is not None and latest_operation.state == "failed":
            projection_state = "blocked"
        elif latest_operation is not None and latest_operation.state in {"pending", "running"}:
            projection_state = (
                "verifying" if latest_operation.kind == "verify" else "building"
            )
        elif settings.gitllery_projection_mode == "active":
            projection_state = "active"
        elif segment_repositories == 0:
            projection_state = "shadow_unbuilt"
        elif legacy_repositories or unplanned_intents or sum(pending.values()):
            projection_state = "shadow_building"
        elif latest_build is not None and latest_build.state in {"staged", "complete"}:
            gates = (latest_build.stats or {}).get("gates") or {}
            projection_state = "shadow_ready" if gates.get("passed") else "awaiting_gates"
        else:
            projection_state = "shadow_ready"

        repositories = []
        missing = behind_total = 0
        for key, descriptor in descriptors.items():
            state = states.get(key)
            recorded_exists = state is not None
            behind = pending.get(key, 0)
            behind_total += behind
            integrity = not bool(state and state.last_error)
            drift: list[str] = [state.last_error] if state and state.last_error else []
            if deep:
                repo = self._segment_repo_for(descriptor)
                exists_on_disk = await asyncio.to_thread(repo.exists)
                if not exists_on_disk:
                    integrity = False
                    drift = ["segment repository is missing from disk"]
                else:
                    verification = await asyncio.to_thread(repo.verify, deep=True)
                    integrity = verification.ok
                    drift = list(verification.errors)
            else:
                exists_on_disk = recorded_exists
            if not exists_on_disk:
                missing += 1
            repositories.append(
                {
                    "repository_id": key,
                    "source": descriptor.source,
                    "creator_dir": descriptor.creator_dir,
                    "exists": exists_on_disk,
                    "behind": behind,
                    "object_integrity_ok": integrity,
                    "drift": drift,
                    "clean": exists_on_disk and behind == 0 and integrity,
                    "product_version": "v1",
                    "format_id": "gitllery-segment",
                    "format_revision": 1,
                    "projection_mode": settings.gitllery_projection_mode,
                    "head_segment": state.head_segment if state else None,
                    "last_complete_commit_id": (
                        str(state.last_complete_commit_id)
                        if state and state.last_complete_commit_id
                        else None
                    ),
                    "last_verified_at": state.last_verified_at if state else None,
                }
            )
        result = {
            "repositories": repositories,
            "missing_repos": missing,
            "behind_total": behind_total,
            "deep": deep,
            "needs_reconcile": False,
            "product_version": "v1",
            "format_id": "gitllery-segment",
            "format_revision": 1,
            "projection_mode": settings.gitllery_projection_mode,
            "unplanned_intents": unplanned_intents,
            "legacy_repositories": legacy_repositories,
            "segment_repositories": segment_repositories,
            "projection_state": projection_state,
            "projection_error": (
                latest_operation.last_error
                if latest_operation is not None
                and latest_operation.state == "failed"
                else None
            ),
            "last_verified_at": last_verified_at,
        }
        if not deep:
            cache_set(key, result, TTL["gitllery:status"])
        return result

    async def reconcile(
        self, repository_id: str | None = None, *, continuation: dict | None = None,
    ) -> dict:
        projected = await self.project_pending(repository_id, continuation=continuation)
        return {"projected": projected, "status": await self.status(repository_id),
                "continuation": self.continuation,
                "successor_delay_seconds": self.successor_delay_seconds}

    async def rebuild_db_from_disk(self, repository_id: str | None = None, dry_run: bool = False) -> dict:
        from app.services.gitllery.rebuild import GitlleryRebuilder
        return await GitlleryRebuilder(self.db).rebuild(repository_id, dry_run)

    async def log(self, repository_id: str, limit: int = 50) -> dict:
        resolver = RepoResolver(self.db)
        desc = next((d for d in await resolver.all_repositories() if d.key() == repository_id), None)
        if desc is None:
            raise HTTPException(status_code=404, detail="repository not found")
        if settings.gitllery_projection_mode.strip().lower() in {"shadow", "active"}:
            repo = self._segment_repo_for(desc)
            if not repo.exists():
                return {"repository_id": repository_id, "entries": [], "total": 0}
            manifest = await asyncio.to_thread(repo.read_manifest)
            commits = await asyncio.to_thread(
                lambda: list(islice(repo.iter_commits(newest_first=True), limit))
            )
            entries = [
                {
                    "commit": commit.get("commit_id"),
                    "db_commit_id": commit.get("commit_id"),
                    "message": commit.get("message", ""),
                    "trigger": commit.get("trigger"),
                    "actor": commit.get("actor_id") or commit.get("actor_type"),
                    "occurred_at": commit.get("occurred_at"),
                    "change_count": len(commit.get("changes") or []),
                }
                for commit in commits
            ]
            return {
                "repository_id": repository_id,
                "entries": entries,
                "total": int(manifest.get("commit_count") or 0),
            }
        repo = self._repo_for(desc)
        entries = []
        cur = repo.head_commit()
        total = 0
        guard = 0
        while cur and guard < 1_000_000:
            guard += 1
            try:
                obj = repo.objects.read(cur)
            except OSError:
                break
            total += 1
            if len(entries) < limit:
                entries.append({
                    "commit": cur,
                    "db_commit_id": obj.get("db_commit_id"),
                    "message": obj.get("message", ""),
                    "trigger": obj.get("trigger"),
                    "actor": obj.get("actor_id") or obj.get("actor_type"),
                    "occurred_at": obj.get("occurred_at"),
                    "change_count": len(obj.get("changes") or []),
                })
            cur = obj.get("parent")
        return {"repository_id": repository_id, "entries": entries, "total": total}

    async def verify_segment_repository(
        self,
        repository_id: str,
        *,
        deep: bool,
    ) -> dict:
        descriptor = next(
            (
                item
                for item in await RepoResolver(self.db).all_repositories()
                if item.key() == repository_id
            ),
            None,
        )
        if descriptor is None:
            raise HTTPException(status_code=404, detail="repository not found")
        repo = self._segment_repo_for(descriptor)
        if not repo.exists():
            raise HTTPException(status_code=404, detail="segment repository not built")
        result = await asyncio.to_thread(repo.verify, deep=deep)
        if result.ok:
            await self.db.execute(
                update(GitlleryRepositoryState)
                .where(GitlleryRepositoryState.repository_key == repository_id)
                .values(last_verified_at=datetime.now(timezone.utc), last_error=None)
            )
            await self.db.commit()
        return {"repository_id": repository_id, "deep": deep, **result.as_dict()}


_CHECKPOINT_KEY = "gitllery:checkpoint"
_CHECKPOINT_CLAMP_SECONDS = 5


def _read_checkpoint() -> tuple[datetime, str] | None:
    """Verified-projected checkpoint P=(created_at, id). Best-effort.

    Invariant: every curation_commit at position <= P has been verified present
    in the on-disk chain of every repo it affects."""
    try:
        import json
        from app.services.redis_client import get_redis
        raw = get_redis().get(_CHECKPOINT_KEY)
        if not raw:
            return None
        data = json.loads(raw)
        return datetime.fromisoformat(data["created_at"]), str(data["id"])
    except Exception:
        logger.debug("gitllery checkpoint read failed", exc_info=True)
        return None


def _advance_checkpoint(created_at: datetime, commit_id) -> bool:
    """Advance P — refuse positions younger than the in-flight-tx clamp window."""
    try:
        now = datetime.now(timezone.utc)
        ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        # A non-positive value explicitly disables the clamp (used by tests
        # and controlled maintenance). Without the guard, tiny database/app
        # clock skew can still reject a checkpoint when the configured window
        # is zero.
        if (
            _CHECKPOINT_CLAMP_SECONDS > 0
            and ts > now - timedelta(seconds=_CHECKPOINT_CLAMP_SECONDS)
        ):
            return False
        import json
        from app.services.redis_client import get_redis
        get_redis().set(_CHECKPOINT_KEY, json.dumps(
            {"created_at": ts.isoformat(), "id": str(commit_id)}))
        return True
    except Exception:
        logger.debug("gitllery checkpoint write failed", exc_info=True)
        return False


def _reset_checkpoint() -> None:
    try:
        from app.services.redis_client import get_redis
        get_redis().delete(_CHECKPOINT_KEY)
    except Exception:
        logger.debug("gitllery checkpoint reset failed", exc_info=True)


def _invalidate_status_cache() -> None:
    """Drop all cached gitllery status payloads (any repo/deep combination)."""
    from app.services.cache import cache_delete_pattern
    cache_delete_pattern("gitllery:status:*")


async def rebuild_checkpoint(
    db: AsyncSession,
    position: tuple[datetime, UUID] | None = None,
) -> bool:
    """Set the checkpoint to an explicitly verified commit position.

    Only valid right after a full successful project_pending (the library is
    fully projected at that instant). Called from the queued reconcile job so
    status() never needs a full-history walk to re-establish P. May return
    False when the newest commit is younger than the in-flight-tx clamp —
    harmless: the next reconcile (or a later clean status) will set it.
    """
    if position is None:
        last = (
            await db.execute(
                select(CurationCommit.created_at, CurationCommit.id)
                .order_by(
                    CurationCommit.created_at.desc(),
                    CurationCommit.id.desc(),
                )
                .limit(1)
            )
        ).first()
        if last is None:
            return False
        position = (last[0], last[1])
    return _advance_checkpoint(position[0], position[1])


async def project_commit_safe(db: AsyncSession, commit_id) -> None:
    """Persist a projection intent without adding filesystem I/O to callers."""
    try:
        cid = commit_id if isinstance(commit_id, UUID) else UUID(str(commit_id))
        from app.services.gitllery_outbox import request_gitllery_projection
        from app.database import async_session

        # Keep the caller's transaction boundary unchanged. Curation callers
        # invoke this after their authoritative commit; the independent short
        # transaction closes the DB->filesystem crash window durably.
        async with async_session() as outbox_db:
            await request_gitllery_projection(outbox_db, cid)
            await outbox_db.commit()
    except Exception:
        logger.warning(
            "gitllery projection intent failed for commit %s",
            commit_id,
            exc_info=True,
        )
