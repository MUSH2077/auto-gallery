# Gitllery Reverse Rebuild (Bundle A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended — this writes to the DB) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `rebuild_db_from_disk` — after `disk-import` re-creates entities with new UUIDs, restore the operator's manual curation (final state + manual commit history) from `.gitllery`, re-mapped by natural keys. Idempotent, transactional, insert-only + set-state, never deletes.

**Architecture:** New `backend/app/services/gitllery/rebuild.py` (`GitlleryRebuilder`) holds the heavy logic; `GitlleryService.rebuild_db_from_disk(...)` delegates. Reuses `CurationService._create_commit`/`_add_change` for idempotent historical-commit insertion (dedupe_key aware). Runs on the operations queue with a dry-run mode. This is the ONE gitllery path that writes the DB.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, FastAPI, RQ, pytest; Next.js panel button.

## Global Constraints

- A writes the DB (`curation_commits`, `curation_changes`, `work_curation_states`, `creator_curation_states`, `asset_storage_states`, `Work.is_favorite`) — insert-only + set-state, one transaction, idempotent, **never deletes**.
- Replay MANUAL commits only. AUTO (skip) = `{"baseline_backfill", "source_synced"}`. MANUAL = everything else (`work_trash`, `work_restore`, `work_favorite`, `creator_*`, `work_purge`, `commit_revert`).
- Idempotency via `dedupe_key = f"rebuild:{original_db_commit_id}"` (reuse `CurationService._create_commit`, which returns the existing commit on dedupe hit).
- Re-mapping anchors: work → `(source, source_work_id)`; creator/repository → each repo's `config.json` (`creator_id`/`repository_id` + `source` + `source_creator_id`); asset → best-effort skip.
- Unmappable subjects are dropped and counted; never an error. No NAS absolute paths in the report. `RequireAdmin` on the endpoint.
- On-disk formats (verified against real data):
  - commit object: `{"type":"commit","db_commit_id","occurred_at","message","trigger","actor_type","actor_id","dedupe_key","stats","reverts","parent","tree","changes":[{"subject_type","subject_id","action","diff","impact"}]}` (inline changes carry diff+impact, NOT before/after).
  - tree: `{"type":"tree","entries":{"<subject_type>/<subject_id>":"<blobhash>"}}`.
  - blob: `{"type":"blob","subject_type","subject_id","state":{...}}` (work state has `source`/`source_creator_id`/`source_work_id`/`visibility`/`is_favorite`/...; creator state has `visibility`/`is_favorite`/`name`/...; asset state has `storage_state`).
  - config.json: `{"schema_version","repository_id","source","creator_id","source_creator_id","creator_dir","object_hash","compression"}`.
- Tests: in-container path `tests/...`; integration uses `@pytest.mark.integration @pytest.mark.asyncio` + `async_session` + `engine.dispose()` in `finally`; monkeypatch `gitllery.service.settings.library_root` (rebuild reads it too — import `settings` in rebuild.py from `app.config`).
- Commit after each task; messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## Reused signatures (do not change)

- `GitlleryRepo(library_root, source, creator_dir)`: `.exists()`, `.head_commit()`, `.read_config()`, `.objects.read(hash)`, `.read_index()`.
- `RepoResolver(db).all_repositories() -> list[RepoDescriptor]` (`.source`,`.source_creator_id`,`.creator_id`,`.creator_dir`,`.repository_id`,`.key()`).
- `CurationService(db)._create_commit(*, message, trigger, actor_type="admin", actor_id=None, reverts_commit_id=None, metadata=None, occurred_at=None, status="active", dedupe_key=None) -> CurationCommit` (dedupe: returns existing if dedupe_key present). `._add_change(commit, *, subject_type, subject_id, action, before_state, after_state, impact=None) -> CurationChange`. `._ensure_work_state(work_id)`, `._ensure_creator_state(creator_id)`, `._ensure_asset_state(asset_id)`.
- Models: `WorkSource(source, source_work_id, source_creator_id, work_id)`, `SourceCreator(source, source_creator_id, creator_id)`, `SubscriptionSource(source, source_creator_id, id)`, `Work(id, is_favorite)`, `WorkCurationState(work_id, visibility, trashed_at, reason)`, `CreatorCurationState(creator_id, visibility, archived_at, reason)`, `AssetStorageState(asset_id, storage_state)`.

---

### Task 1: On-disk collector (`rebuild.py`) — merge commits by `db_commit_id`

**Files:** Create `backend/app/services/gitllery/rebuild.py`; Test `backend/tests/test_gitllery_rebuild.py`.

**Interfaces (Produces):**
- `AUTO_TRIGGERS = {"baseline_backfill", "source_synced"}`.
- `@dataclass MergedCommit`: `db_commit_id: str`, `occurred_at: str|None`, `message: str`, `trigger: str`, `actor_type: str|None`, `actor_id: str|None`, `stats: dict`, `reverts: str|None`, `changes: list[dict]` (unioned; each `{subject_type,subject_id,action,diff,impact,after_state}`), property `is_manual: bool`.
- `class GitlleryRebuilder(db)` with `async def _iter_repos() -> list[tuple[GitlleryRepo, dict]]` (repo + config) — uses `RepoResolver(self.db).all_repositories()` + `settings.library_root`; and `def _collect_merged_commits(repos) -> list[MergedCommit]` — walks each repo chain, groups by `db_commit_id`, unions `changes` (dedupe by `(db_cid, subject_type, subject_id, action)`), and for each change resolves its `after_state` from that commit's own tree blob (`repo.objects.read(commit["tree"]).entries["<st>/<sid>"]` → blob `state`), preserving inline `diff`/`impact`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_gitllery_rebuild.py
import uuid
import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, asset_sources, assets, "
        "work_sources, works, source_creators, subscription_sources, subscriptions, "
        "creators, work_curation_states, creator_curation_states, asset_storage_states "
        "RESTART IDENTITY CASCADE"))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collect_merges_commits_by_db_commit_id(tmp_path, monkeypatch):
    """Two repos each holding a slice of the same DB commit merge into one
    MergedCommit with the union of changes; auto triggers are flagged."""
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        db_cid = str(uuid.uuid4())
        for cdir, sid in [("alice", "w-a"), ("bob", "w-b")]:
            repo = GitlleryRepo(tmp_path, "pixiv", cdir)
            repo.init({"schema_version": 1, "source": "pixiv", "creator_dir": cdir,
                       "creator_id": str(uuid.uuid4()), "source_creator_id": cdir,
                       "repository_id": str(uuid.uuid4())}, f"pixiv/{cdir}")
            blob = repo.objects.write({"type": "blob", "subject_type": "work",
                                       "subject_id": sid, "state": {"visibility": "trashed"}})
            tree = repo.objects.write({"type": "tree", "entries": {f"work/{sid}": blob}})
            commit = repo.objects.write({
                "type": "commit", "tree": tree, "parent": None, "db_commit_id": db_cid,
                "actor_type": "admin", "actor_id": None, "message": "trash 1",
                "trigger": "work_trash", "dedupe_key": None, "reverts": None,
                "occurred_at": "2026-06-30T10:00:00+00:00", "stats": {},
                "changes": [{"subject_type": "work", "subject_id": sid,
                             "action": "work_trashed", "diff": {}, "impact": {}}]})
            repo.set_head(commit, actor="admin", message="trash 1")

        async with async_session() as db:
            await _clear(db)
            rebuilder = rb.GitlleryRebuilder(db)
            repos = [(GitlleryRepo(tmp_path, "pixiv", "alice"), {}),
                     (GitlleryRepo(tmp_path, "pixiv", "bob"), {})]
            merged = rebuilder._collect_merged_commits(repos)
            assert len(merged) == 1
            mc = merged[0]
            assert mc.db_commit_id == db_cid
            assert mc.trigger == "work_trash" and mc.is_manual is True
            assert sorted(c["subject_id"] for c in mc.changes) == ["w-a", "w-b"]
            assert all(c["after_state"] == {"visibility": "trashed"} for c in mc.changes)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d postgres redis && docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_rebuild.py::test_collect_merges_commits_by_db_commit_id -v`
Expected: FAIL — `ModuleNotFoundError: app.services.gitllery.rebuild`.

- [ ] **Step 3: Implement the collector**

```python
# backend/app/services/gitllery/rebuild.py
"""Gitllery reverse rebuild — restore manual curation from .gitllery into the DB.

The ONE gitllery path that writes the DB. Insert-only + set-state, idempotent
(dedupe_key="rebuild:{db_commit_id}"), transactional, never deletes.
"""
from __future__ import annotations

import logging
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
                except OSError:
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
                    tree = repo.objects.read(obj["tree"]) if obj.get("tree") else {"entries": {}}
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
                            except OSError:
                                after_state = None
                        mc.changes.append({
                            "subject_type": st, "subject_id": sid, "action": action,
                            "diff": ch.get("diff") or {}, "impact": ch.get("impact") or {},
                            "after_state": after_state,
                        })
                cur = obj.get("parent")
        return sorted(merged.values(), key=lambda m: (m.occurred_at or "", m.db_commit_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_rebuild.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/rebuild.py backend/tests/test_gitllery_rebuild.py
git commit -m "feat(gitllery): rebuild collector — merge on-disk commits by db_commit_id

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Entity re-mapping (old UUID → current UUID)

**Files:** Modify `backend/app/services/gitllery/rebuild.py`; Test append.

**Interfaces (Produces on `GitlleryRebuilder`):**
- `async def _build_maps(self, repos, merged) -> dict` returning `{"work": dict[str,str], "creator": dict[str,str], "repository": dict[str,str]}` (old subject UUID → current entity UUID string). Work map: from any change's `after_state` carrying `(source, source_work_id)` on the work itself, or `(work_id, source, source_work_id)` on a repository/source_synced change → `(source, source_work_id)` → current `WorkSource.work_id`. Creator/repository maps: from each repo `config.json` (`creator_id`/`repository_id` + `source` + `source_creator_id`) → current `SourceCreator.creator_id` / `SubscriptionSource.id`.

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_maps_remaps_work_and_creator(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.rebuild import MergedCommit
    from app.models import Creator, SourceCreator, Work, WorkSource

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()

            old_work = str(uuid.uuid4()); old_creator = str(uuid.uuid4())
            cfg = {"creator_id": old_creator, "source": "pixiv", "source_creator_id": "123",
                   "repository_id": str(uuid.uuid4())}
            merged = [MergedCommit(
                db_commit_id=str(uuid.uuid4()), occurred_at=None, message="", trigger="work_trash",
                actor_type=None, actor_id=None, stats={}, reverts=None,
                changes=[{"subject_type": "work", "subject_id": old_work, "action": "work_trashed",
                          "diff": {}, "impact": {},
                          "after_state": {"source": "pixiv", "source_work_id": "9001"}}])]
            maps = await rb.GitlleryRebuilder(db)._build_maps([(None, cfg)], merged)
            assert maps["work"][old_work] == str(work.id)
            assert maps["creator"][old_creator] == str(creator.id)
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

- [ ] **Step 2: Run to verify it fails** — `AttributeError: _build_maps`.

- [ ] **Step 3: Implement `_build_maps`**

```python
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
```

- [ ] **Step 4: Run to verify it passes** — `docker compose run ... pytest tests/test_gitllery_rebuild.py -v` → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/rebuild.py backend/tests/test_gitllery_rebuild.py
git commit -m "feat(gitllery): rebuild entity re-mapping (work/creator/repository)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `rebuild()` orchestration + `GitlleryService.rebuild_db_from_disk`

**Files:** Modify `backend/app/services/gitllery/rebuild.py`, `backend/app/services/gitllery/service.py`; Test append (full loop).

**Interfaces (Produces):**
- `GitlleryRebuilder.rebuild(self, repository_id=None, dry_run=False) -> dict` — report `{"commits_restored","commits_skipped_auto","commits_deduped","changes_unmapped","states_applied","dry_run"}`.
- `GitlleryService.rebuild_db_from_disk(repository_id=None, dry_run=False)` delegating to `GitlleryRebuilder`.

Algorithm: collect merged commits (T1) → build maps (T2) → for each MANUAL merged commit in `occurred_at` order, re-map change subject_ids (drop unmappable, count them); if `dry_run`, tally only; else insert via `CurationService._create_commit(..., occurred_at=<orig>, dedupe_key=f"rebuild:{db_commit_id}", reverts_commit_id=<mapped>)` + `._add_change(...)` per re-mapped change. Distinguish first-insert vs dedupe hit via `_commit_already_had_changes` BEFORE adding changes. Track `orig_db_commit_id → new commit.id` for `reverts`. Then apply final state from each repo's latest tree blobs (re-mapped) to state tables. Commit unless dry_run.

- [ ] **Step 1: Write the failing full-loop test (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_rebuild_restores_state_and_manual_commits_idempotently(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery import rebuild as rb
    from app.services.gitllery.service import GitlleryService
    from app.models import Creator, SourceCreator, Work, WorkSource
    from app.models import CurationCommit, WorkCurationState
    from sqlalchemy import select, func

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    monkeypatch.setattr(rb.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w"); db.add(work); await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

        async with async_session() as db:
            await _clear(db)
            creator2 = Creator(name="七诗"); db.add(creator2); await db.flush()
            db.add(SourceCreator(creator_id=creator2.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work2 = Work(title="w"); db.add(work2); await db.flush()
            db.add(WorkSource(work_id=work2.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()
            new_work_id = work2.id

        async with async_session() as db:
            report = await GitlleryService(db).rebuild_db_from_disk()
            assert report["commits_restored"] >= 1
        async with async_session() as db:
            st = (await db.execute(select(WorkCurationState).where(
                WorkCurationState.work_id == new_work_id))).scalar_one_or_none()
            assert st is not None and st.visibility == "trashed"
            manual = (await db.execute(select(func.count(CurationCommit.id)).where(
                CurationCommit.trigger == "work_trash"))).scalar()
            assert manual >= 1

        async with async_session() as db:
            await GitlleryService(db).rebuild_db_from_disk()
        async with async_session() as db:
            manual2 = (await db.execute(select(func.count(CurationCommit.id)).where(
                CurationCommit.trigger == "work_trash"))).scalar()
            assert manual2 == manual
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

- [ ] **Step 2: Run to verify it fails** — `AttributeError: rebuild_db_from_disk`.

- [ ] **Step 3: Implement `rebuild()` + `_apply_final_state` + delegate**

Add to `rebuild.py`:

```python
    async def rebuild(self, repository_id: str | None = None, dry_run: bool = False) -> dict:
        from datetime import datetime
        from uuid import UUID
        from app.services.curation import CurationService

        repos = await self._iter_repos()
        if repository_id is not None:
            repos = [(r, c) for (r, c) in repos if (c or {}).get("repository_id") == repository_id]
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

        report["states_applied"] = await self._apply_final_state(repos, maps, dry_run)
        if not dry_run:
            await self.db.commit()
        return report

    async def _commit_already_had_changes(self, commit_id) -> bool:
        from sqlalchemy import select, func
        from app.models import CurationChange
        n = (await self.db.execute(select(func.count(CurationChange.id)).where(
            CurationChange.commit_id == commit_id))).scalar()
        return bool(n)

    async def _apply_final_state(self, repos, maps, dry_run) -> int:
        from uuid import UUID
        from app.services.curation import CurationService, VISIBLE
        from app.models import Work
        cur = CurationService(self.db)
        applied = 0
        for repo, _cfg in repos:
            head = repo.head_commit()
            if not head:
                continue
            head_obj = repo.objects.read(head)
            tree = repo.objects.read(head_obj["tree"]) if head_obj.get("tree") else {"entries": {}}
            for key, blob_hash in (tree.get("entries") or {}).items():
                st, _, old_sid = key.partition("/")
                state = (repo.objects.read(blob_hash).get("state") or {})
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
        return applied
```

Add to `service.py`:

```python
    async def rebuild_db_from_disk(self, repository_id: str | None = None, dry_run: bool = False) -> dict:
        from app.services.gitllery.rebuild import GitlleryRebuilder
        return await GitlleryRebuilder(self.db).rebuild(repository_id, dry_run)
```

- [ ] **Step 4: Run the rebuild suite + regression**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_rebuild.py tests/test_gitllery_service.py tests/test_gitllery_slicing.py -v`
Expected: rebuild 3 passed; service/slicing unchanged (no regression).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/rebuild.py backend/app/services/gitllery/service.py backend/tests/test_gitllery_rebuild.py
git commit -m "feat(gitllery): rebuild_db_from_disk — restore manual curation state + history

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Schemas + API endpoint + operations job

**Files:** Modify `backend/app/schemas/gitllery.py`, `backend/app/api/curation.py`, `backend/app/jobs/admin_operations.py`; Test `backend/tests/test_gitllery_api.py` (append).

**Interfaces:**
- `GitlleryRebuildResponse` schema: `commits_restored:int`, `commits_skipped_auto:int`, `commits_deduped:int`, `changes_unmapped:int`, `states_applied:int`, `dry_run:bool`, `job_id:str|None=None`, `status:str|None=None`.
- `POST /api/v1/curation/gitllery/rebuild` (`RequireAdmin`) with query `dry_run: bool = True`, `repository_id: str | None = None`: `dry_run=True` → run inline `await GitlleryService(db).rebuild_db_from_disk(repository_id, dry_run=True)` and return the report; `dry_run=False` → enqueue an operations job (mirror `import_from_disk` at `backend/app/api/admin.py:1122-1172`: redis lock `library:gitllery-rebuild:active`, `TaskService.create_task(operation_type="admin-gitllery-rebuild", title="Rebuild curation from disk", queue_name="operations")`, enqueue `app.jobs.admin_operations.run_gitllery_rebuild_operation`) and return `{"status":"enqueued","job_id":...}`.
- `run_gitllery_rebuild_operation(job_id, options)` + `_run_gitllery_rebuild_operation` in `admin_operations.py` — copy the shape of `_run_disk_import_operation` (`admin_operations.py:148-210`), calling `await GitlleryService(db).rebuild_db_from_disk(options.get("repository_id"))`, with TaskService status transitions and the redis `library:gitllery-rebuild:active` cleanup in `finally`.

- [ ] **Step 1: Write the test (append to test_gitllery_api.py)** — seed a work, trash it, project; then wipe+recreate the work/creator with new uuids; then `POST /api/v1/curation/gitllery/rebuild?dry_run=true` → assert 200 and `commits_restored >= 1` and body has `dry_run: true`. Override `get_admin_key` (as the existing gitllery API test does).
- [ ] **Step 2: Run** → FAIL 404.
- [ ] **Step 3: Implement** the schema, the route (inline dry-run + enqueue real), and the operations job (mirror the named `_run_disk_import_operation`).
- [ ] **Step 4: Run** `tests/test_gitllery_api.py` → passing.
- [ ] **Step 5: Commit** `feat(gitllery): rebuild API endpoint + operations job`.

---

### Task 5: Admin-web "Rebuild from disk" action

**Files:** Modify `admin-web/src/lib/api/index.ts` (+ types), `admin-web/src/components/GitlleryPanel.tsx`, `admin-web/src/lib/i18n.tsx`.

- [ ] **Step 1:** Read `admin-web/src/lib/api/index.ts` (request helper + `queryKeys.gitllery`) and `GitlleryPanel.tsx`.
- [ ] **Step 2:** Add `gitlleryRebuild: (dryRun: boolean, repositoryId?: string) => request<T.GitlleryRebuildReport>("/curation/gitllery/rebuild?dry_run=" + dryRun + (repositoryId ? "&repository_id=" + encodeURIComponent(repositoryId) : ""), { method: "POST" })` and the `GitlleryRebuildReport` type.
- [ ] **Step 3:** In `GitlleryPanel`, add a "Rebuild from disk" button: on click call `gitlleryRebuild(true)` (dry-run), show the returned counts in a `confirm()` ("will restore N commits, M states; K unmapped — proceed?"); on confirm call `gitlleryRebuild(false)` (mutation → toast + `qc.invalidateQueries({ queryKey: queryKeys.gitllery.all })`). Static tone classes only.
- [ ] **Step 4:** i18n keys (`gitllery.rebuild`, `gitllery.rebuild_confirm`, `gitllery.rebuild_done`) in every locale. `cd admin-web && npm run build` → succeeds.
- [ ] **Step 5:** Commit `feat(admin-web): gitllery rebuild-from-disk action`.

---

### Task 6: Verify + deploy

- [ ] **Step 1:** Full gitllery suite green: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_objects.py tests/test_gitllery_repo.py tests/test_gitllery_slicing.py tests/test_gitllery_service.py tests/test_gitllery_api.py tests/test_gitllery_rebuild.py -v`.
- [ ] **Step 2:** Deploy: `docker compose build --build-arg CACHEBUST="$(date +%s)" backend admin-web && docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web`.
- [ ] **Step 3:** Live **dry-run** smoke-check against the current 10 pixiv repos (no manual curation yet → `commits_restored=0`, `states_applied` = entity count, `changes_unmapped=0`); confirm dry-run writes nothing (curation_commits count unchanged).

## Self-Review

**Spec coverage:** collector/merge → T1; re-mapping → T2; orchestration + state apply + idempotency → T3; API/job → T4; frontend → T5; verify/deploy → T6. Asset best-effort skip is in `_apply_final_state` (no asset branch) and in change re-mapping (asset subjects unmapped → counted). Out-of-scope items untouched.

**Placeholder scan:** T1–T3 carry complete code. T4/T5 are mechanical (mirror the named existing `_run_disk_import_operation` / api client / panel patterns) with concrete endpoints, field names, and the exact file+lines to copy from — implementation-by-analogy, not placeholders.

**Type consistency:** `MergedCommit.changes` dicts carry `after_state` (added by the collector) consumed by `_build_maps`/`rebuild`/`_apply_final_state`; `maps` shape `{"work","creator","repository"}` used consistently; `_create_commit`/`_add_change` calls match the reused signatures; `dedupe_key="rebuild:{db_commit_id}"` consistent between insert and idempotency; `AUTO_TRIGGERS` gates `is_manual`.

**Risk note (flag for T3 review):** the dedupe-vs-first-insert branch. `_create_commit` returns the existing commit on a dedupe hit AND a fresh commit on first insert; the plan distinguishes them via `_commit_already_had_changes(commit.id)` (a dedupe hit already has its changes, a fresh commit has none yet). The reviewer must confirm this skips re-adding changes on the second run while adding them on the first (the idempotency test covers it). If found fragile, replace with an explicit `select(CurationCommit).where(dedupe_key==...)` existence check before `_create_commit`.
