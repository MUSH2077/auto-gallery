"""GitlleryService — project the authoritative curation DAG onto disk.

Read-only on the database; all mutation is filesystem. Best-effort and
idempotent: re-projecting a commit is a no-op, a missed projection is caught up.
"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

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
        return affected


async def project_commit_safe(db: AsyncSession, commit_id) -> None:
    """Best-effort projection: never raises into the caller's request/job."""
    try:
        cid = commit_id if isinstance(commit_id, UUID) else UUID(str(commit_id))
        await GitlleryService(db).project_commit(cid)
    except Exception:
        logger.warning("gitllery projection failed for commit %s", commit_id, exc_info=True)
