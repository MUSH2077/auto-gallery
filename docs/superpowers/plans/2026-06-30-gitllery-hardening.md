# Gitllery Hardening (Bundle B) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Three small hardening fixes to the shipped gitllery projection — kill query amplification (B1), deep blob integrity (B2), 404 for unknown repository_id (B3).

**Architecture:** Confined to `backend/app/services/gitllery/` + tests. No schema change, no new API surface (only 200→404). DB authoritative; projection stays one-way, DB-read-only.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, FastAPI, pytest.

## Global Constraints

- DB authoritative; `.gitllery` one-way projection; `CurationService` write methods unchanged.
- No NAS absolute paths in any API response; projection best-effort and DB-read-only.
- Tests run in-container as `tests/...` (bind mount maps host `backend/` → `/app`): `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest <args> -v`. Integration tests need Postgres (`docker compose up -d postgres redis`) and use `@pytest.mark.integration @pytest.mark.asyncio` with `app.database.async_session` + `engine.dispose()` in `finally`.
- Commit after each task; end every commit message with `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1 (B1): Cache `_repos_for_change` per `(subject_type, subject_id)`

**Files:**
- Modify: `backend/app/services/gitllery/slicing.py`
- Test: `backend/tests/test_gitllery_slicing.py` (append)

**Interfaces:**
- `RepoResolver.__init__` gains `self._change_repos_cache: dict[tuple[str, str], list[RepoDescriptor]] = {}`.
- `_repos_for_change(change)` becomes a caching wrapper delegating to a new `_resolve_repos_for_change(change)` holding the original body. Public `slice_changes`/`all_repositories` signatures unchanged.

- [ ] **Step 1: Write the failing test (append to test_gitllery_slicing.py)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_repos_for_change_is_cached_per_subject():
    """The same (subject_type, subject_id) is resolved against the DB once, even
    across many changes — kills O(commits x changes) query amplification."""
    from app.database import async_session, engine
    from app.models import Creator, SourceCreator, Work, WorkSource, CurationChange
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w")
            db.add(work)
            await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()

            resolver = RepoResolver(db)
            calls = {"n": 0}
            original = resolver._resolve_repos_for_change

            async def counting(change):
                calls["n"] += 1
                return await original(change)
            resolver._resolve_repos_for_change = counting

            changes = [
                CurationChange(commit_id=uuid.uuid4(), subject_type="work",
                               subject_id=str(work.id), action=a,
                               before_state=None, after_state={"visibility": v})
                for a, v in [("work_trashed", "trashed"), ("work_restored", "visible"),
                             ("work_favorited", "visible")]
            ]
            sliced = await resolver.slice_changes(changes)
            # 3 changes on the same work → resolved against the DB exactly once.
            assert calls["n"] == 1
            # Correctness preserved: still exactly the one pixiv repo.
            assert len(sliced) == 1
            desc, _ = next(iter(sliced.values()))
            assert desc.source == "pixiv"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d postgres redis && docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_slicing.py::test_repos_for_change_is_cached_per_subject -v`
Expected: FAIL — `AttributeError: 'RepoResolver' object has no attribute '_resolve_repos_for_change'`.

- [ ] **Step 3: Implement the cache**

In `RepoResolver.__init__`, add the cache field (after the existing caches):

```python
        self._change_repos_cache: dict[tuple[str, str], list[RepoDescriptor]] = {}
```

Rename the current `_repos_for_change` method body to `_resolve_repos_for_change`, and add a caching wrapper. Concretely, replace:

```python
    async def _repos_for_change(self, change: CurationChange) -> list[RepoDescriptor]:
        st, sid = change.subject_type, change.subject_id
        if st == "work":
```

with:

```python
    async def _repos_for_change(self, change: CurationChange) -> list[RepoDescriptor]:
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
```

(the rest of the original method body — the `work`/`asset`/`creator`/`repository` branches and the trailing `return []` — stays unchanged, now under `_resolve_repos_for_change`).

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_slicing.py -v`
Expected: 4 passed (3 existing + new).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/slicing.py backend/tests/test_gitllery_slicing.py
git commit -m "perf(gitllery): cache _repos_for_change per (subject_type,subject_id)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2 (B2): Deep blob integrity in `_integrity_ok`

**Files:**
- Modify: `backend/app/services/gitllery/service.py`
- Test: `backend/tests/test_gitllery_service.py` (append)

**Interfaces:** `_integrity_ok(repo)` unchanged signature; blob check upgraded from `exists()` to `verify()`.

- [ ] **Step 1: Write the failing test (append to test_gitllery_service.py)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_integrity_detects_corrupt_blob(tmp_path, monkeypatch):
    from app.database import async_session, engine
    from app.services.curation import CurationService
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService
    from app.services.gitllery.repo import GitlleryRepo

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            creator, work = await _seed_work(db)
            commit = await CurationService(db).trash_works([work.id], message="trash 1")
            await db.commit()
            await GitlleryService(db).project_commit(commit.id)

            repo = GitlleryRepo(tmp_path, "pixiv", "七诗")
            assert GitlleryService(db)._integrity_ok(repo) is True

            # Corrupt one tree-referenced blob's stored bytes.
            head = repo.head_commit()
            tree_hash = repo.objects.read(head)["tree"]
            blob_hash = next(iter(repo.objects.read(tree_hash)["entries"].values()))
            (repo.root / "objects" / blob_hash[:2] / blob_hash[2:]).write_bytes(b"corrupt-not-zlib")

            assert GitlleryService(db)._integrity_ok(repo) is False
            s = await GitlleryService(db).status()
            assert any(r["object_integrity_ok"] is False for r in s["repositories"])
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

Note: match the file's existing `finally` teardown convention (local `from app.database import async_session, engine` + `await engine.dispose()`) exactly as the other tests in the file do.

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_service.py::test_integrity_detects_corrupt_blob -v`
Expected: FAIL — `_integrity_ok` currently returns True for a corrupt-but-present blob (the `is False` assertion fails).

- [ ] **Step 3: Implement — verify blobs**

In `service.py`, in `_integrity_ok`, change the blob loop from `exists` to `verify`:

```python
        for blob_hash in (repo.objects.read(tree_hash).get("entries") or {}).values():
            if not repo.objects.verify(blob_hash):
                return False
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_service.py -v`
Expected: all passing (existing + new).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/service.py backend/tests/test_gitllery_service.py
git commit -m "fix(gitllery): verify (not just exists) blobs in _integrity_ok

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3 (B3): 404 for unknown `repository_id` in `status`/`log`

**Files:**
- Modify: `backend/app/services/gitllery/service.py`
- Test: `backend/tests/test_gitllery_service.py` (append)

**Interfaces:** `status(repository_id)` and `log(repository_id)` raise `HTTPException(404)` when `repository_id` is provided and unknown. Library-wide `status()` unchanged. Add `from fastapi import HTTPException` to service.py imports.

- [ ] **Step 1: Write the failing test (append)**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_status_and_log_404_for_unknown_repository(tmp_path, monkeypatch):
    import uuid as _uuid
    import pytest as _pytest
    from fastapi import HTTPException
    from app.database import async_session, engine
    from app.services.gitllery import service as gsvc
    from app.services.gitllery.service import GitlleryService

    monkeypatch.setattr(gsvc.settings, "library_root", str(tmp_path))
    try:
        async with async_session() as db:
            await _clear(db)
            unknown = str(_uuid.uuid4())
            with _pytest.raises(HTTPException) as ei:
                await GitlleryService(db).status(unknown)
            assert ei.value.status_code == 404
            with _pytest.raises(HTTPException) as ei2:
                await GitlleryService(db).log(unknown)
            assert ei2.value.status_code == 404
            # Library-wide status (no id) must NOT 404.
            s = await GitlleryService(db).status()
            assert "repositories" in s
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_service.py::test_status_and_log_404_for_unknown_repository -v`
Expected: FAIL — today `status(unknown)` returns an empty structure and `log(unknown)` returns empty entries (no `HTTPException`).

- [ ] **Step 3: Implement — 404 guard**

Add the import near the top of `service.py`:

```python
from fastapi import HTTPException
```

In `status`, guard at the very top of the method body:

```python
    async def status(self, repository_id: str | None = None) -> dict:
        if repository_id is not None:
            known = {d.key() for d in await RepoResolver(self.db).all_repositories()}
            if repository_id not in known:
                raise HTTPException(status_code=404, detail="repository not found")
        # ... existing body unchanged ...
```

In `log`, replace the `desc is None → return empty` branch with a 404:

```python
    async def log(self, repository_id: str, limit: int = 50) -> dict:
        resolver = RepoResolver(self.db)
        desc = next((d for d in await resolver.all_repositories() if d.key() == repository_id), None)
        if desc is None:
            raise HTTPException(status_code=404, detail="repository not found")
        # ... existing body unchanged ...
```

- [ ] **Step 4: Run the full gitllery suite**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_slicing.py tests/test_gitllery_service.py tests/test_gitllery_api.py -v`
Expected: all passing. (The API test hits library-wide status/reconcile, which is unaffected.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/gitllery/service.py backend/tests/test_gitllery_service.py
git commit -m "feat(gitllery): 404 for unknown repository_id in status/log

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Verify + deploy

- [ ] **Step 1: Full gitllery suite green**

Run: `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_objects.py tests/test_gitllery_repo.py tests/test_gitllery_slicing.py tests/test_gitllery_service.py tests/test_gitllery_api.py -v`
Expected: all passing.

- [ ] **Step 2: Deploy (backend only — no frontend change)**

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" backend
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler
```

- [ ] **Step 3: Smoke-check**

Confirm services healthy and `GitlleryService.status()` still returns the 10 pixiv repos clean; a request for a random unknown `repository_id` now 404s.

## Self-Review

**Spec coverage:** B1 → Task 1, B2 → Task 2, B3 → Task 3, verify/deploy → Task 4. Out-of-scope bundles A/C/D untouched.

**Placeholder scan:** no TBD; every step shows the concrete edit. Task 2 Step 1's teardown note instructs matching the file's existing `finally` pattern — a real instruction, not a placeholder.

**Type consistency:** `_change_repos_cache` keyed by `tuple[str, str]` (subject_type, subject_id), values `list[RepoDescriptor]` — matches `_repos_for_change` return type. `HTTPException(status_code=404)` consistent across `status`/`log`. `verify()` returns `bool`, used in a boolean guard like the `exists()` it replaces.
