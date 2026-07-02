"""Gitllery reverse rebuild — restore manual curation from .gitllery into the DB.

The ONE gitllery path that writes the DB. Insert-only + set-state, idempotent
(dedupe_key="rebuild:{db_commit_id}"), transactional, never deletes.
"""
from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.gitllery.repo import GitlleryRepo
from app.services.gitllery.slicing import RepoResolver

logger = logging.getLogger(__name__)

AUTO_TRIGGERS = {"baseline_backfill", "source_synced"}


@dataclass
class MergedCommit:
    db_commit_id: str
    occurred_at: str | None
    message: str
    trigger: str
    actor_type: str | None
    actor_id: str | None
    stats: dict
    reverts: str | None
    changes: list[dict] = field(default_factory=list)

    @property
    def is_manual(self) -> bool:
        return self.trigger not in AUTO_TRIGGERS


class GitlleryRebuilder:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _iter_repos(self) -> list[tuple[GitlleryRepo, dict]]:
        out: list[tuple[GitlleryRepo, dict]] = []
        for desc in await RepoResolver(self.db).all_repositories():
            repo = GitlleryRepo(Path(settings.library_root), desc.source, desc.creator_dir)
            if repo.exists():
                out.append((repo, repo.read_config()))
        return out

    def _collect_merged_commits(self, repos: list[tuple[GitlleryRepo, dict]]) -> list[MergedCommit]:
        merged: dict[str, MergedCommit] = {}
        seen_change: set[tuple[str, str, str, str]] = set()  # (db_cid, st, sid, action)
        for repo, _cfg in repos:
            cur = repo.head_commit()
            guard = 0
            while cur and guard < 1_000_000:
                guard += 1
                try:
                    obj = repo.objects.read(cur)
                except (OSError, ValueError, zlib.error):
                    break
                db_cid = obj.get("db_commit_id")
                if db_cid:
                    mc = merged.get(db_cid)
                    if mc is None:
                        mc = MergedCommit(
                            db_commit_id=db_cid, occurred_at=obj.get("occurred_at"),
                            message=obj.get("message", ""), trigger=obj.get("trigger", ""),
                            actor_type=obj.get("actor_type"), actor_id=obj.get("actor_id"),
                            stats=obj.get("stats") or {}, reverts=obj.get("reverts"))
                        merged[db_cid] = mc
                    try:
                        tree = repo.objects.read(obj["tree"]) if obj.get("tree") else {"entries": {}}
                    except (OSError, ValueError, zlib.error):
                        tree = {"entries": {}}
                    entries = tree.get("entries") or {}
                    for ch in obj.get("changes", []):
                        st, sid, action = ch.get("subject_type"), ch.get("subject_id"), ch.get("action")
                        dedup = (db_cid, st, sid, action)
                        if dedup in seen_change:
                            continue
                        seen_change.add(dedup)
                        after_state = None
                        blob_hash = entries.get(f"{st}/{sid}")
                        if blob_hash:
                            try:
                                after_state = repo.objects.read(blob_hash).get("state")
                            except (OSError, ValueError, zlib.error):
                                after_state = None
                        mc.changes.append({
                            "subject_type": st, "subject_id": sid, "action": action,
                            "diff": ch.get("diff") or {}, "impact": ch.get("impact") or {},
                            "after_state": after_state,
                        })
                cur = obj.get("parent")
        return sorted(merged.values(), key=lambda m: (m.occurred_at or "", m.db_commit_id))

    async def _build_maps(self, repos, merged) -> dict:
        from sqlalchemy import select
        from app.models import WorkSource, SourceCreator, SubscriptionSource

        work_nat: dict[str, tuple[str, str]] = {}
        for mc in merged:
            for ch in mc.changes:
                st, sid, after = ch["subject_type"], ch["subject_id"], (ch.get("after_state") or {})
                if st == "work" and after.get("source") and after.get("source_work_id"):
                    work_nat.setdefault(sid, (after["source"], str(after["source_work_id"])))
                if after.get("work_id") and after.get("source") and after.get("source_work_id"):
                    work_nat.setdefault(str(after["work_id"]),
                                        (after["source"], str(after["source_work_id"])))

        work_map: dict[str, str] = {}
        for old_uuid, (source, swid) in work_nat.items():
            row = await self.db.execute(select(WorkSource.work_id).where(
                WorkSource.source == source, WorkSource.source_work_id == swid))
            wid = row.scalar_one_or_none()
            if wid:
                work_map[old_uuid] = str(wid)

        creator_map: dict[str, str] = {}
        repo_map: dict[str, str] = {}
        for _repo, cfg in repos:
            cfg = cfg or {}
            source, scid = cfg.get("source"), cfg.get("source_creator_id")
            if not (source and scid):
                continue
            old_creator = cfg.get("creator_id")
            if old_creator and old_creator not in creator_map:
                row = await self.db.execute(select(SourceCreator.creator_id).where(
                    SourceCreator.source == source, SourceCreator.source_creator_id == scid))
                cid = row.scalar_one_or_none()
                if cid:
                    creator_map[old_creator] = str(cid)
            old_repo = cfg.get("repository_id")
            if old_repo and old_repo not in repo_map:
                row = await self.db.execute(select(SubscriptionSource.id).where(
                    SubscriptionSource.source == source, SubscriptionSource.source_creator_id == scid))
                rid = row.scalar_one_or_none()
                if rid:
                    repo_map[old_repo] = str(rid)
        return {"work": work_map, "creator": creator_map, "repository": repo_map}
