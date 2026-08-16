"""Gitllery reverse rebuild — restore manual curation from .gitllery into the DB.

The ONE gitllery path that writes the DB. Insert-only + set-state, idempotent
(dedupe_key="rebuild:{db_commit_id}"), transactional, never deletes.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.gitllery.repo import GITLLERY_DIRNAME, GitlleryRepo

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
        """Discover .gitllery repos by scanning LIBRARY_ROOT directly.

        Rebuild is a recovery path: the DISK is the source of truth, so repo
        discovery must not depend on DB-derived creator_dir resolution — after a
        disk-import the DB's raw_metadata may be missing/different and would
        resolve to the wrong directory. Each repo is self-describing via its
        config.json, so a filesystem scan is both simpler and correct.
        """
        root = Path(settings.library_root)
        out: list[tuple[GitlleryRepo, dict]] = []
        if not root.is_dir():
            return out
        for gitllery_dir in sorted(root.glob(f"*/*/{GITLLERY_DIRNAME}")):
            creator_root = gitllery_dir.parent
            try:
                repo = GitlleryRepo(root, creator_root.parent.name, creator_root.name)
            except ValueError:
                continue  # path containment violation — skip
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
                # Skip commits that were themselves inserted by a previous
                # rebuild (later projected back to disk under a NEW
                # db_commit_id) — re-ingesting them would duplicate manual
                # history on every rebuild→project→rebuild cycle. The original
                # commit is still in the chain, so nothing is lost.
                if (obj.get("dedupe_key") or "").startswith("rebuild:"):
                    cur = obj.get("parent")
                    continue
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
                # subscription_sources has NO unique (source, source_creator_id)
                # constraint (unlike work_sources / source_creators), so duplicates
                # across subscriptions are possible — limit(1) avoids
                # MultipleResultsFound and keeps "unmappable → drop, not error".
                row = await self.db.execute(select(SubscriptionSource.id).where(
                    SubscriptionSource.source == source,
                    SubscriptionSource.source_creator_id == scid).limit(1))
                rid = row.scalar_one_or_none()
                if rid:
                    repo_map[old_repo] = str(rid)
        return {"work": work_map, "creator": creator_map, "repository": repo_map}

    async def rebuild(self, repository_id: str | None = None, dry_run: bool = False) -> dict:
        from datetime import datetime
        from uuid import UUID
        from app.services.curation import CurationService

        if not dry_run:
            await self._assert_recovery_context()
        repos = await self._iter_repos()
        if repository_id is not None:
            repos = await self._filter_repos(repos, repository_id)
        merged = self._collect_merged_commits(repos)
        maps = await self._build_maps(repos, merged)
        cur = CurationService(self.db)
        report = {"commits_restored": 0, "commits_skipped_auto": 0, "commits_deduped": 0,
                  "changes_unmapped": 0, "states_applied": 0, "dry_run": dry_run}
        old_to_new_commit: dict[str, str] = {}

        for mc in merged:
            if not mc.is_manual:
                report["commits_skipped_auto"] += 1
                continue
            remapped = []
            for ch in mc.changes:
                new_sid = maps.get(ch["subject_type"], {}).get(ch["subject_id"])
                if new_sid is None:
                    report["changes_unmapped"] += 1
                    continue
                remapped.append({**ch, "subject_id": new_sid})
            if not remapped:
                continue
            if dry_run:
                report["commits_restored"] += 1
                continue
            occurred = None
            if mc.occurred_at:
                try:
                    occurred = datetime.fromisoformat(mc.occurred_at.replace("Z", "+00:00"))
                except ValueError:
                    occurred = None
            reverts_new = old_to_new_commit.get(mc.reverts) if mc.reverts else None
            commit = await cur._create_commit(
                message=mc.message, trigger=mc.trigger,
                actor_type=mc.actor_type or "system", actor_id=mc.actor_id,
                reverts_commit_id=UUID(reverts_new) if reverts_new else None,
                metadata={"rebuilt_from": mc.db_commit_id}, occurred_at=occurred,
                dedupe_key=f"rebuild:{mc.db_commit_id}")
            old_to_new_commit[mc.db_commit_id] = str(commit.id)
            if await self._commit_already_had_changes(commit.id):
                report["commits_deduped"] += 1
                continue
            for ch in remapped:
                await cur._add_change(
                    commit, subject_type=ch["subject_type"], subject_id=ch["subject_id"],
                    action=ch["action"], before_state=None, after_state=ch.get("after_state"),
                    impact=ch.get("impact"))
            commit.stats = mc.stats
            report["commits_restored"] += 1

        states_applied, changed_work_ids = await self._apply_final_state(
            repos,
            maps,
            dry_run,
        )
        report["states_applied"] = states_applied
        if not dry_run:
            if changed_work_ids:
                from app.services.search_projection_outbox import (
                    request_search_projection,
                )

                await request_search_projection(self.db, changed_work_ids)
            await self.db.commit()
            from app.services.gitllery.service import _invalidate_status_cache, _reset_checkpoint
            _invalidate_status_cache()
            # Rebuilt rows' created_at can predate this transaction's commit
            # instant — reset the checkpoint so status can never skip them.
            _reset_checkpoint()
        return report

    async def _assert_recovery_context(self) -> None:
        """Refuse a real rebuild when the DB already holds LIVE manual curation.

        rebuild is a post-disaster tool (scenario A1: disk-import onto a fresh
        database first, then rebuild). Running it against a healthy DB whose
        disk is merely behind would silently regress state tables to older
        on-disk state. Commits from a previous rebuild (dedupe_key
        "rebuild:...") are expected and ignored, so idempotent re-runs stay
        allowed. dry_run is read-only and is not gated.
        """
        from fastapi import HTTPException
        from sqlalchemy import func, or_, select
        from app.models import CurationCommit
        n = (await self.db.execute(
            select(func.count(CurationCommit.id)).where(
                CurationCommit.trigger.notin_(AUTO_TRIGGERS),
                or_(CurationCommit.dedupe_key.is_(None),
                    ~CurationCommit.dedupe_key.like("rebuild:%")),
            ))).scalar()
        if n:
            raise HTTPException(
                status_code=409,
                detail="database already contains live manual curation; "
                       "rebuild_db_from_disk is a post-disaster recovery tool "
                       "(run disk-import on a fresh database first)")

    async def _filter_repos(self, repos, repository_id: str):
        """Filter repos by repository id: match the config's (old, pre-loss)
        repository_id directly, OR resolve a CURRENT subscription_source id to
        its (source, source_creator_id) and match the config on that — callers
        normally only know current ids."""
        from uuid import UUID
        from sqlalchemy import select
        from app.models import SubscriptionSource
        current = None
        try:
            row = await self.db.execute(select(SubscriptionSource).where(
                SubscriptionSource.id == UUID(repository_id)))
            current = row.scalar_one_or_none()
        except (ValueError, TypeError):
            current = None
        out = []
        for r, c in repos:
            cfg = c or {}
            if cfg.get("repository_id") == repository_id:
                out.append((r, c))
            elif (current is not None
                  and cfg.get("source") == current.source
                  and cfg.get("source_creator_id") == current.source_creator_id):
                out.append((r, c))
        return out

    async def _commit_already_had_changes(self, commit_id) -> bool:
        from sqlalchemy import select, func
        from app.models import CurationChange
        n = (await self.db.execute(select(func.count(CurationChange.id)).where(
            CurationChange.commit_id == commit_id))).scalar()
        return bool(n)

    async def _apply_final_state(self, repos, maps, dry_run) -> tuple[int, set[UUID]]:
        from uuid import UUID
        from app.services.curation import CurationService, VISIBLE
        from app.models import Work
        cur = CurationService(self.db)
        applied = 0
        changed_work_ids: set[UUID] = set()
        for repo, _cfg in repos:
            head = repo.head_commit()
            if not head:
                continue
            # Same corrupt-object tolerance as the collector: this is a disaster-
            # recovery path, so one corrupt repo/blob must not abort the whole
            # rebuild (skip the repo / the entry instead).
            try:
                head_obj = repo.objects.read(head)
                tree = repo.objects.read(head_obj["tree"]) if head_obj.get("tree") else {"entries": {}}
            except (OSError, ValueError, zlib.error):
                continue
            for key, blob_hash in (tree.get("entries") or {}).items():
                st, _, old_sid = key.partition("/")
                try:
                    state = (repo.objects.read(blob_hash).get("state") or {})
                except (OSError, ValueError, zlib.error):
                    continue
                if st == "work":
                    new_id = maps["work"].get(old_sid)
                    if not new_id:
                        continue
                    if dry_run:
                        applied += 1; continue
                    ws = await cur._ensure_work_state(UUID(new_id))
                    ws.visibility = state.get("visibility", VISIBLE)
                    ws.reason = state.get("reason")
                    work = await self.db.get(Work, UUID(new_id))
                    if work is not None:
                        work.is_favorite = bool(state.get("is_favorite", False))
                    changed_work_ids.add(UUID(new_id))
                    applied += 1
                elif st == "creator":
                    new_id = maps["creator"].get(old_sid)
                    if not new_id:
                        continue
                    if dry_run:
                        applied += 1; continue
                    cs = await cur._ensure_creator_state(UUID(new_id))
                    cs.visibility = state.get("visibility", VISIBLE)
                    cs.reason = state.get("reason")
                    applied += 1
                # asset: best-effort skip (no natural key)
        return applied, changed_work_ids
