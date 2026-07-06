# Gitllery Status Checkpoint (方案二) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline) or subagent-driven. Checkbox steps.

**Goal:** status() becomes O(repos + tail) at any library size: Redis checkpoint `P=(created_at,id)` + tail-only slicing + bounded HEAD-backwards chain walks + scoped work_sources preload. No migrations, no API change.

**Architecture:** All in `services/gitllery/service.py` (module helpers `_read_checkpoint/_advance_checkpoint/_reset_checkpoint`, service methods `_tail_commits/_changes_for_commits/_pending_count_tail/_projected_tail_ids`, status() branch) + `slicing.py` (`preload_work_sources(work_ids=None)`) + `rebuild.py` (reset hook) + gitllery test fixtures.

## Global Constraints

- Checkpoint invariant: **everything ≤ P is verified present in every affected repo's chain.** Axis = `created_at` (+id tiebreak). Advance ONLY from a library-wide (`repository_id is None`) status that verified `behind_total == 0`, clamped to positions older than `now()−5s`. Per-repo status never advances P. deep=True never READS P (full walk) but may advance it. Absent/corrupt P → full walk fallback that re-sets P (self-healing). rebuild (non-dry-run) resets P.
- Best-effort Redis (failures → fallback, never raise). Test landmine: checkpoint persists in redis db 15 — extend the existing autouse fixtures to delete it. Clamp is monkeypatched to 0 in tests (`gsvc._CHECKPOINT_CLAMP_SECONDS`).
- Tests in-container `tests/...`; commit per task; trailer `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1: checkpoint helpers + scoped preload (unit-testable primitives)

**Files:** `service.py`, `slicing.py`; tests append to `test_gitllery_service.py` + `test_gitllery_slicing.py`.

- [ ] **Step 1 (RED):** tests:
  - `test_checkpoint_helpers_clamp_and_roundtrip` (service tests): `_advance_checkpoint(now, uuid4())` → False and `_read_checkpoint()` None; `_advance_checkpoint(now−10s, cid)` → True and `_read_checkpoint() == (ts, str(cid))`; `_reset_checkpoint()` → None again. (Fixture already cleans the key.)
  - `test_preload_work_sources_scoped` (slicing tests): seed two works; scoped preload to `[str(w1.id)]` → slicing a w1 change resolves its repo; scoped preload to `[]` → slicing a w1 change returns `{}` (nothing resolvable — scoping semantics). RED: attributes/kwarg missing.
- [ ] **Step 2:** implement.
  - `service.py` imports add `from datetime import datetime, timedelta, timezone`; module constants + helpers:
    ```python
    _CHECKPOINT_KEY = "gitllery:checkpoint"
    _CHECKPOINT_CLAMP_SECONDS = 5


    def _read_checkpoint() -> tuple[datetime, str] | None:
        """Verified-projected checkpoint P=(created_at, id). Best-effort."""
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
        """Advance P — refuse positions younger than the in-flight-tx clamp."""
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
    ```
  - `slicing.py` `preload_work_sources` gains `work_ids: list[str] | None = None`: `None` → load all (unchanged); `[]` → set `{}` and return; list → `WHERE work_id IN (validated UUIDs)` (skip unparseable ids).
  - Autouse fixtures in the three gitllery test files also call `service._reset_checkpoint()`.
- [ ] **Step 3 (GREEN):** targeted tests pass; slicing suite no regression.
- [ ] **Step 4: Commit** `feat(gitllery): checkpoint primitives + scoped work_sources preload`.

---

### Task 2: status() tail fast path

**Files:** `service.py`; tests append to `test_gitllery_service.py`.

- [ ] **Step 1 (RED):** tests (all monkeypatch `gsvc._CHECKPOINT_CLAMP_SECONDS = 0` via setattr and clear response cache between calls):
  - `test_status_checkpoint_fastpath_and_deep_bypass`: seed+trash+project → `status()` (full walk, clean) sets P (`_read_checkpoint() is not None`); restore+project (new commit) → clear response cache → monkeypatch-count `_all_changes_by_commit` → `status()` → count==0 (tail path), behind 0, P advanced (position > previous); then `status(deep=True)` (cache cleared) → count==1 (bypass).
  - `test_status_checkpoint_blocks_on_missed_projection`: with P set (from clean status), create a commit WITHOUT projecting, clear cache → `status()` → `behind_total == 1`, P unchanged; also `status(repository_id=<real repo id>)` (clean for that repo) must NOT advance P.
  - `test_status_checkpoint_catchup_ordering`: P set at c0; create c1, c2; project ONLY c2 (`project_commit`), clear cache → status → behind 1 (c1), P unchanged; `project_pending()` then clear cache → status → behind 0, P advanced to c2's position.
- [ ] **Step 2:** implement in `service.py`:
  - Tail loaders:
    ```python
    async def _tail_commits(self, checkpoint: tuple[datetime, str]) -> list[CurationCommit]:
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
    ```
  - Bounded walk (staticmethod):
    ```python
    @staticmethod
    def _projected_tail_ids(repo: GitlleryRepo, tail_ids: set[str]) -> set[str]:
        """Walk from HEAD, stop at the first db_commit_id outside the tail set —
        exact under the checkpoint invariant (≤P already in chains; post-P
        projections are all tail ids; chains are append-only)."""
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
    ```
  - Tail counter:
    ```python
    async def _pending_count_tail(self, repository_id: str | None, tail: list[CurationCommit]):
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
    ```
  - `status()` branch — replace the single `_pending_count_by_repo` call (after cache-get):
    ```python
        checkpoint = None if deep else _read_checkpoint()
        max_pos = None
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
    ```
    and just before `cache_set`:
    ```python
        if repository_id is None and behind_total == 0 and max_pos is not None:
            _advance_checkpoint(max_pos[0], max_pos[1])
    ```
- [ ] **Step 3 (GREEN):** service+api suites pass (existing status tests unaffected: fixtures reset P → full-walk path preserved).
- [ ] **Step 4: Commit** `perf(gitllery): O(repos+tail) status via verified checkpoint`.

---

### Task 3: rebuild resets the checkpoint

**Files:** `rebuild.py`; test append to `test_gitllery_rebuild.py`.

- [ ] **Step 1 (RED):** `test_rebuild_resets_checkpoint`: monkeypatch clamp 0; `_advance_checkpoint(now−10s, uuid4())`; run `GitlleryRebuilder(db).rebuild()` on an empty library_root (no repos; empty DB passes the recovery gate) → `_read_checkpoint() is None`.
- [ ] **Step 2:** in `rebuild()` where the cache is invalidated after commit:
    ```python
            from app.services.gitllery.service import _invalidate_status_cache, _reset_checkpoint
            _invalidate_status_cache()
            _reset_checkpoint()
    ```
- [ ] **Step 3 (GREEN):** rebuild suite passes.
- [ ] **Step 4: Commit** `fix(gitllery): rebuild resets the status checkpoint`.

---

### Task 4: verify + measure + deploy-ask

- [ ] Full gitllery family + full backend suite (expect only the known flake).
- [ ] Measure (real 711-commit data): clear cache+checkpoint → full-walk miss (≈638ms, sets P) → clear cache only → **tail-empty miss (target ≤50ms)** → hit ~0.3ms. Record.
- [ ] Ledger + report; deploy with user go-ahead.

## Self-Review

**Spec coverage:** invariant/axis/clamp → T1; fast path incl. bounded walk + scoped preload + self-advance + per-repo-no-advance + deep bypass → T2; rebuild reset → T3; fallback = T2's else-branch (absent P → full walk + re-set). Every spec failure-mode row has a test except redis-down (helpers are try/except best-effort by construction).
**Placeholders:** none — full code in T1/T2/T3.
**Type consistency:** checkpoint tuple `(datetime, str)` produced by `_read_checkpoint`, consumed by `_tail_commits`; `max_pos` `(datetime, UUID)` fed to `_advance_checkpoint(created_at, commit_id)`; `preload_work_sources(work_ids: list[str]|None)` with `[]→{}` short-circuit; `_projected_tail_ids` returns `set[str]` matching `projected_cache` element type.
**Risk notes:** (1) advancement guarded by `repository_id is None` — per-repo status returns max_pos but never advances; (2) the deep-bypass counter test catches any accidental tail-path fallthrough; (3) existing status tests keep passing because fixtures delete P (full-walk path).
