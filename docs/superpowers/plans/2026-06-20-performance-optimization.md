# Performance Optimization — Implementation Plan

**Goal:** Eliminate backend memory starvation (99.97%), reduce polling load, add API caching.

**Architecture:** 5 config-level changes — no new code paths, no new dependencies.

**Tech Stack:** Docker Compose, Next.js TanStack Query, FastAPI

## Global Constraints

- No new dependencies
- All changes are single-line config/code adjustments
- Deploy with `bash scripts/deploy.sh`

---

### Task 1: Backend Memory + Health Intervals

**Files:** `docker-compose.yaml`

- [ ] **Step 1:** backend mem_limit `512M` → `1024M`
- [ ] **Step 2:** All healthcheck interval `20s`/`30s` → `60s` (5 occurrences)
- [ ] **Step 3:** Commit

---

### Task 2: Frontend Polling

**Files:** `admin-web/src/app/admin/jobs/page.tsx:14-15`

- [ ] **Step 1:** `REFETCH_ACTIVE_MS = 3000` → `8000`
- [ ] **Step 2:** `REFETCH_IDLE_MS = 10000` → `30000`
- [ ] **Step 3:** Commit

---

### Task 3: Workbench API Cache

**Files:** `backend/app/api/system.py`

Add 30s TTL memory cache to workbench_summary:

```python
_workbench_cache = None
_workbench_cache_ts = 0.0
_WORKBENCH_CACHE_TTL = 30.0

@router.get("/system/workbench")
async def workbench_summary(db=Depends(get_db)):
    global _workbench_cache, _workbench_cache_ts
    now = time.monotonic()
    if _workbench_cache is not None and (now - _workbench_cache_ts) < _WORKBENCH_CACHE_TTL:
        return _workbench_cache
    # ... existing workbench logic unchanged ...
    _workbench_cache = result
    _workbench_cache_ts = now
    return result
```

- [ ] **Step 1:** Add cache variables before function
- [ ] **Step 2:** Add cache check at function start, cache store at end
- [ ] **Step 3:** Commit

---

### Task 4: DB Pool Shrink

**Files:** `backend/app/database.py:13-14`

- [ ] **Step 1:** `pool_size=3` → `pool_size=2`
- [ ] **Step 2:** `max_overflow=3` → `max_overflow=2`
- [ ] **Step 3:** Commit

---

### Task 5: Build + Verify

- [ ] **Step 1:** `bash scripts/deploy.sh`
- [ ] **Step 2:** `docker stats --no-stream | grep backend` → memory < 80%
- [ ] **Step 3:** Final commit
