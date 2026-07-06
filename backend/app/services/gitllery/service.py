"""GitlleryService — project the authoritative curation DAG onto disk.

Read-only on the database; all mutation is filesystem. Best-effort and
idempotent: re-projecting a commit is a no-op, a missed projection is caught up.
"""
from __future__ import annotations

import asyncio
import logging
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CurationChange, CurationCommit
from app.services.gitllery.repo import GitlleryRepo, SCHEMA_VERSION
from app.services.gitllery.slicing import RepoDescriptor, RepoResolver
from app.services.locks import redis_lock

logger = logging.getLogger(__name__)


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

    async def _changes_for_commit(self, commit_id: UUID) -> list[CurationChange]:
        rows = await self.db.execute(
            select(CurationChange).where(CurationChange.commit_id == commit_id)
            .order_by(CurationChange.created_at, CurationChange.id))
        return list(rows.scalars().all())

    def _repo_for(self, desc: RepoDescriptor) -> GitlleryRepo:
        return GitlleryRepo(Path(settings.library_root), desc.source, desc.creator_dir)

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
            },
            f"{desc.source} / {desc.creator_dir} curation history",
        )

    def _apply_commit_to_repo(
        self, repo: GitlleryRepo, desc: RepoDescriptor,
        commit: CurationCommit, changes: list[CurationChange],
    ) -> str | None:
        """Write blobs+tree+commit for one repo. Returns new commit hash, or None if already projected."""
        self._ensure_init(repo, desc)
        # Authoritative idempotency guard: this re-reads HEAD's chain under the
        # caller's per-repo redis_lock, so even if the lock expired mid-pass or a
        # concurrent projector advanced HEAD, an already-projected commit is never
        # re-appended. Any pre-lock cached check (e.g. in project_pending) is only
        # an optimization; this is the check that actually prevents duplication.
        if str(commit.id) in repo.projected_db_commit_ids():
            return None

        index = repo.read_index()
        entries: dict[str, str] = dict((index.get("tree_entries") or {}))
        for change in changes:
            entity_key = f"{change.subject_type}/{change.subject_id}"
            after = change.after_state
            if after is None:
                entries.pop(entity_key, None)
                index.setdefault("entities", {}).pop(entity_key, None)
            else:
                blob_hash = repo.objects.write(
                    {"type": "blob", "subject_type": change.subject_type,
                     "subject_id": change.subject_id, "state": after})
                entries[entity_key] = blob_hash
                index.setdefault("entities", {})[entity_key] = after

        tree_hash = repo.objects.write({"type": "tree", "entries": entries})
        commit_hash = repo.objects.write({
            "type": "commit",
            "tree": tree_hash,
            "parent": repo.head_commit(),
            "db_commit_id": str(commit.id),
            "actor_type": commit.actor_type,
            "actor_id": commit.actor_id,
            "message": commit.message,
            "trigger": commit.trigger,
            "dedupe_key": commit.dedupe_key,
            "reverts": str(commit.reverts_commit_id) if commit.reverts_commit_id else None,
            "occurred_at": commit.occurred_at.isoformat() if commit.occurred_at else None,
            "stats": commit.stats or {},
            "changes": [_change_payload(c) for c in changes],
        })
        actor = commit.actor_id or commit.actor_type or "system"
        repo.set_head(commit_hash, actor=actor, message=commit.message)
        index["head"] = commit_hash
        index["tree"] = tree_hash
        index["tree_entries"] = entries
        repo.write_index(index)
        return commit_hash

    async def _ordered_commits(self) -> list[CurationCommit]:
        rows = await self.db.execute(
            select(CurationCommit).order_by(
                CurationCommit.occurred_at, CurationCommit.created_at, CurationCommit.id))
        return list(rows.scalars().all())

    async def project_pending(self, repository_id: str | None = None) -> dict[str, int]:
        resolver = RepoResolver(self.db)
        await resolver.preload_work_sources()
        counts: dict[str, int] = {}
        # repo_id -> (GitlleryRepo, RepoDescriptor, projected db_commit_id set)
        repo_cache: dict[str, tuple[GitlleryRepo, RepoDescriptor, set[str]]] = {}
        changes_by_commit = await self._all_changes_by_commit()
        for commit in await self._ordered_commits():
            changes = changes_by_commit.get(commit.id, [])
            sliced = await resolver.slice_changes(changes)
            for rid, (desc, repo_changes) in sliced.items():
                if repository_id is not None and rid != repository_id:
                    continue
                if rid not in repo_cache:
                    repo = self._repo_for(desc)
                    self._ensure_init(repo, desc)
                    repo_cache[rid] = (repo, desc, repo.projected_db_commit_ids())
                repo, desc_cached, projected = repo_cache[rid]
                if str(commit.id) in projected:
                    continue
                # Bulk path (reconcile/backfill): a single commit can fan out to
                # thousands of blob writes, so allow a longer hold than the
                # single-commit project_commit (60s). Lock expiry is still safe —
                # the object store is content-addressed and the under-lock HEAD
                # re-check in _apply_commit_to_repo prevents any duplication.
                async with redis_lock(f"gitllery:{rid}", ttl_seconds=300) as acquired:
                    if not acquired:
                        continue
                    new_hash = self._apply_commit_to_repo(repo, desc_cached, commit, repo_changes)
                if new_hash is not None:
                    projected.add(str(commit.id))
                    counts[rid] = counts.get(rid, 0) + 1
        if counts:
            _invalidate_status_cache()
        return counts

    async def backfill(self) -> dict[str, int]:
        # Ensure every source-creator repo exists, then project all commits.
        resolver = RepoResolver(self.db)
        for desc in await resolver.all_repositories():
            self._ensure_init(self._repo_for(desc), desc)
        return await self.project_pending()

    async def project_commit(self, commit_id: UUID) -> list[str]:
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
                    continue
                repo = self._repo_for(desc)
                if self._apply_commit_to_repo(repo, desc, commit, repo_changes) is not None:
                    affected.append(repository_id)
        if affected:
            _invalidate_status_cache()
        return affected

    async def _all_changes_by_commit(self) -> dict[UUID, list[CurationChange]]:
        """One query for ALL changes grouped by commit — replaces the per-commit
        _changes_for_commit N+1 in bulk walks (status / project_pending)."""
        rows = await self.db.execute(
            select(CurationChange).order_by(CurationChange.created_at, CurationChange.id))
        grouped: dict[UUID, list[CurationChange]] = {}
        for ch in rows.scalars().all():
            grouped.setdefault(ch.commit_id, []).append(ch)
        return grouped

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

    async def _pending_count_by_repo(self, repository_id: str | None) -> tuple[dict[str, int], dict[str, RepoDescriptor]]:
        resolver = RepoResolver(self.db)
        await resolver.preload_work_sources()
        projected_cache: dict[str, set[str]] = {}
        repo_desc: dict[str, RepoDescriptor] = {}
        pending: dict[str, int] = {}
        changes_by_commit = await self._all_changes_by_commit()
        for commit in await self._ordered_commits():
            changes = changes_by_commit.get(commit.id, [])
            sliced = await resolver.slice_changes(changes)
            for rid, (desc, _changes) in sliced.items():
                if repository_id is not None and rid != repository_id:
                    continue
                repo_desc[rid] = desc
                if rid not in projected_cache:
                    repo = self._repo_for(desc)
                    # Disk chain walk (zlib per object) — keep it off the event loop.
                    projected_cache[rid] = (
                        await asyncio.to_thread(repo.projected_db_commit_ids)
                        if repo.exists() else set())
                if str(commit.id) not in projected_cache[rid]:
                    pending[rid] = pending.get(rid, 0) + 1
        for rid in repo_desc:
            pending.setdefault(rid, 0)
        return pending, repo_desc

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
        for blob_hash in (repo.objects.read(tree_hash).get("entries") or {}).values():
            if not repo.objects.verify(blob_hash):
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
        # position). deep is the ground-truth path and never reads P.
        checkpoint = None if deep else _read_checkpoint()
        max_pos: tuple | None = None
        if checkpoint is not None:
            tail = await self._tail_commits(checkpoint)
            pending, repo_desc, max_pos = await self._pending_count_tail(repository_id, tail)
        else:
            pending, repo_desc = await self._pending_count_by_repo(repository_id)
            last = (await self.db.execute(
                select(CurationCommit.created_at, CurationCommit.id)
                .order_by(CurationCommit.created_at.desc(), CurationCommit.id.desc())
                .limit(1))).first()
            max_pos = (last[0], last[1]) if last else None
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
                "clean": exists and behind == 0 and integrity and not drift,
            })
        # Only a LIBRARY-WIDE clean result verifies the invariant for every
        # repo — per-repo status must never advance P.
        if repository_id is None and behind_total == 0 and max_pos is not None:
            _advance_checkpoint(max_pos[0], max_pos[1])
        result = {"repositories": repos_out, "missing_repos": missing,
                  "behind_total": behind_total, "deep": deep}
        cache_set(key, result, TTL["gitllery:status"])
        return result

    async def reconcile(self, repository_id: str | None = None) -> dict:
        projected = await self.project_pending(repository_id)
        return {"projected": projected, "status": await self.status(repository_id)}

    async def rebuild_db_from_disk(self, repository_id: str | None = None, dry_run: bool = False) -> dict:
        from app.services.gitllery.rebuild import GitlleryRebuilder
        return await GitlleryRebuilder(self.db).rebuild(repository_id, dry_run)

    async def log(self, repository_id: str, limit: int = 50) -> dict:
        resolver = RepoResolver(self.db)
        desc = next((d for d in await resolver.all_repositories() if d.key() == repository_id), None)
        if desc is None:
            raise HTTPException(status_code=404, detail="repository not found")
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
        if ts > now - timedelta(seconds=_CHECKPOINT_CLAMP_SECONDS):
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


async def project_commit_safe(db: AsyncSession, commit_id) -> None:
    """Best-effort projection: never raises into the caller's request/job."""
    try:
        cid = commit_id if isinstance(commit_id, UUID) else UUID(str(commit_id))
        await GitlleryService(db).project_commit(cid)
    except Exception:
        logger.warning("gitllery projection failed for commit %s", commit_id, exc_info=True)
