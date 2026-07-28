# NAS Latency Optimization (方案一) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Kill the measured 2.3–3.4s `gitllery.status()` hot path (batch + light mode + threads + cache), stop the frontend from hammering it, and ship an 8GB NAS resource profile.

**Architecture:** All backend changes confined to `services/gitllery/service.py` (+ `rebuild.py` invalidation hook, `schemas/gitllery.py`, `api/curation.py` deep param) and `services/cache.py` (TTL entry). Frontend: two files. Deploy: base compose redis policy + a new `docker-compose.nas8g.yaml` override.

**Tech Stack:** Python 3.12 / SQLAlchemy async / FastAPI; Next.js 14 / TanStack Query; Docker Compose.

## Global Constraints

- Projection stays best-effort + DB-read-only; no NAS paths in responses; `RequireAdmin` unchanged.
- Cache is best-effort (services/cache.py already degrades on redis failure).
- Tests in-container path `tests/...`; integration tests use `async_session` + `engine.dispose()` finally; **new landmine:** the status cache persists across tests (redis db 15) — every gitllery test file gets an autouse fixture clearing `gitllery:status:*`.
- Commit per task; messages end `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1 (P1a–c): status fast-path — batch loader, light/deep, off-loop disk

**Files:** Modify `backend/app/services/gitllery/service.py`, `backend/app/schemas/gitllery.py`, `backend/app/api/curation.py`; Test `backend/tests/test_gitllery_service.py`.

- [ ] **Step 1: Failing tests.** In `test_gitllery_service.py`: (i) new `test_status_uses_batched_changes_loader`: seed+project one commit (reuse `_seed_work` + trash + project pattern), monkeypatch a counting wrapper on `GitlleryService._changes_for_commit`, call `status()`, assert count == 0 and `s["deep"] is False`; (ii) update `test_integrity_detects_corrupt_blob`: after corrupting the blob, assert light `status()` reports the repo still `clean is True` (probe only reads the HEAD commit object) AND `status(deep=True)` reports `object_integrity_ok is False`. Run → RED (`deep` kwarg unknown / `_changes_for_commit` called).

- [ ] **Step 2: Implement in `service.py`.**
  - Add imports: `import asyncio`, `import zlib`.
  - Add batched loader:
    ```python
    async def _all_changes_by_commit(self) -> dict[UUID, list[CurationChange]]:
        """One query for ALL changes grouped by commit — replaces the per-commit
        _changes_for_commit N+1 in bulk walks (status / project_pending)."""
        rows = await self.db.execute(
            select(CurationChange).order_by(CurationChange.created_at, CurationChange.id))
        grouped: dict[UUID, list[CurationChange]] = {}
        for ch in rows.scalars().all():
            grouped.setdefault(ch.commit_id, []).append(ch)
        return grouped
    ```
  - `_pending_count_by_repo`: load `changes_by_commit = await self._all_changes_by_commit()` once before the loop; inside use `changes = changes_by_commit.get(commit.id, [])`; wrap the disk walk: `projected_cache[rid] = (await asyncio.to_thread(repo.projected_db_commit_ids)) if repo.exists() else set()`.
  - `project_pending`: same batched substitution (loader once before its loop).
  - Add light probe:
    ```python
    def _integrity_probe(self, repo: GitlleryRepo) -> bool:
        """Light-mode check: HEAD resolves and its commit object loads."""
        head = repo.head_commit()
        if head is None:
            return True
        try:
            return repo.objects.read(head).get("type") == "commit"
        except (OSError, ValueError, zlib.error):
            return False
    ```
  - `status(self, repository_id=None, deep: bool = False)`: per-repo
    ```python
    integrity = (await asyncio.to_thread(self._integrity_ok if deep else self._integrity_probe, repo)) if exists else True
    drift = (await self._drift_keys(repo)) if (deep and exists) else []
    ```
    and the return dict gains `"deep": deep`.
  - Schema `GitlleryStatusResponse`: add `deep: bool = False`.
  - Routes: both status endpoints gain `deep: bool = Query(False)` and pass it through. `reconcile` keeps default light.

- [ ] **Step 3: GREEN.** `docker compose run --rm -T -v "$PWD/backend:/app" backend python -m pytest tests/test_gitllery_service.py tests/test_gitllery_api.py -v` → all pass.
- [ ] **Step 4: Commit** `perf(gitllery): status fast-path — batched changes, light/deep, off-loop disk`.

---

### Task 2 (P1d): Redis cache + invalidation

**Files:** Modify `backend/app/services/cache.py` (TTL entry), `backend/app/services/gitllery/service.py`, `backend/app/services/gitllery/rebuild.py`; Test `backend/tests/test_gitllery_service.py` (+ autouse fixtures in all gitllery test files).

- [ ] **Step 1: Autouse cache-clear fixture** (cross-test landmine) — add to `test_gitllery_service.py`, `test_gitllery_rebuild.py`, `test_gitllery_api.py`:
  ```python
  @pytest.fixture(autouse=True)
  def _clear_gitllery_status_cache():
      from app.services.cache import cache_delete_pattern
      cache_delete_pattern("gitllery:status:*")
      yield
  ```
- [ ] **Step 2: Failing test.** `test_status_cached_and_invalidated_on_projection`: seed+trash+project; monkeypatch counting wrapper on `GitlleryService._pending_count_by_repo`; call `status()` twice → compute count == 1 and payloads equal; then create a second manual commit (restore the work) + `project_commit` → third `status()` recomputes (count == 2). Run → RED (no cache yet → count == 2 after two calls).
- [ ] **Step 3: Implement.**
  - `cache.py`: add `"gitllery:status": 30,` to the `TTL` registry.
  - `service.py` `status()`: after the 404 guard, wrap compute:
    ```python
    from app.services.cache import TTL, cache_get, cache_key, cache_set
    key = cache_key("gitllery:status", repository_id=repository_id, deep=deep)
    cached = cache_get(key)
    if cached is not None:
        return cached
    ...existing compute into `result`...
    cache_set(key, result, TTL["gitllery:status"])
    return result
    ```
    (Note: the per-repo 404 guard runs before the cache and costs an `all_repositories()` pass — acceptable; the hot panel path is repository_id=None which skips it.)
  - Module helper in `service.py`:
    ```python
    def _invalidate_status_cache() -> None:
        from app.services.cache import cache_delete_pattern
        cache_delete_pattern("gitllery:status:*")
    ```
    Call after successful writes: end of `project_commit` (`if affected:`), end of `project_pending` (`if counts:`). (`backfill`/`reconcile` delegate to `project_pending` → covered.)
  - `rebuild.py` `rebuild()`: after `await self.db.commit()` add
    ```python
    from app.services.gitllery.service import _invalidate_status_cache
    _invalidate_status_cache()
    ```
- [ ] **Step 4: GREEN** — full gitllery family: `tests/test_gitllery_*.py` all pass (fixtures keep isolation).
- [ ] **Step 5: Commit** `perf(gitllery): cache status (TTL 30s) + invalidate on projection/rebuild`.

---

### Task 3 (P2): frontend query hygiene

**Files:** Modify `admin-web/src/components/GitlleryPanel.tsx`, `admin-web/src/app/admin/creators/[id]/page.tsx`.

- [ ] **Step 1:** GitlleryPanel status query → `staleTime: 60_000, refetchOnWindowFocus: false`.
- [ ] **Step 2:** Creator page: `import { POLL_IDLE_MS } from "@/lib/polling";` timeline/stats queries → `refetchInterval: POLL_IDLE_MS, staleTime: POLL_IDLE_MS` (was hard 15000).
- [ ] **Step 3:** `cd admin-web && npm run build` → 0 errors.
- [ ] **Step 4: Commit** `perf(admin-web): calm gitllery/status polling (staleTime, adaptive idle)`.

---

### Task 4 (P3): redis policy + 8GB NAS profile + doc

**Files:** Modify `docker-compose.yaml` (redis command only); Create `docker-compose.nas8g.yaml`, `docs/deployment-profiles.md`.

- [ ] **Step 1:** Base compose redis command: `--maxmemory 128mb --maxmemory-policy noeviction` (was `64mb` / `allkeys-lru`) — RQ queues must never be silently evicted.
- [ ] **Step 2:** `docker-compose.nas8g.yaml` override (check base's limit syntax first — `grep -n "mem_limit\|memory" docker-compose.yaml` — and mirror it): meilisearch 256M (new), worker-import 1G + command `imports 1`, worker-download 512M, backend 768M. Header comment documents usage.
- [ ] **Step 3:** `docs/deployment-profiles.md` — short bilingual note: when to use, the exact `docker compose -f docker-compose.yaml -f docker-compose.nas8g.yaml up -d` line, what it changes.
- [ ] **Step 4:** Validate `docker compose -f docker-compose.yaml -f docker-compose.nas8g.yaml config -q` exits 0.
- [ ] **Step 5: Commit** `feat(deploy): redis noeviction + 8GB NAS resource profile`.

---

### Task 5: verify + measure + deploy

- [ ] **Step 1:** Full gitllery suite + full backend suite (expect only the known `test_disk_import` flake).
- [ ] **Step 2:** Re-measure: in-container timing of `status()` (miss + hit) — target: miss ≤ ~300ms, hit ≤ ~10ms (vs 2337–3438ms baseline). Record numbers.
- [ ] **Step 3:** Deploy (rebuild backend+admin-web, recreate services incl. redis for the new policy) — with user go-ahead — then live re-measure once.

## Self-Review

**Spec coverage:** P1a→T1 batch, P1b→T1 light/deep, P1c→T1 to_thread, P1d→T2, P2→T3, P3→T4, verification/measurement→T5. Out-of-scope untouched.
**Placeholder scan:** T1/T2 carry the exact code; T3/T4 are two-line config edits with exact values; T4 Step 2 has a verify-before-edit instruction for the compose limit syntax (real instruction, not a placeholder).
**Type consistency:** `status(repository_id: str|None, deep: bool)` threaded through route→service→schema (`deep: bool = False` in all three); cache key includes both params; `_all_changes_by_commit` returns `dict[UUID, list[CurationChange]]` consumed with `.get(commit.id, [])`; invalidation helper is module-level so `rebuild.py` can import it lazily inside the function (no circular import — service.py does not import rebuild at module level).
**Risk notes:** (1) cached dict returned straight to FastAPI — `GitlleryStatusResponse` re-validates it, and `cache_set` JSON round-trips via `_to_json`, so types stay JSON-safe; (2) the corrupt-blob light assertion depends on the probe reading only the HEAD commit object — if HEAD itself were the corrupt object the light probe DOES catch it (stricter, fine); the test corrupts a blob, not HEAD.
