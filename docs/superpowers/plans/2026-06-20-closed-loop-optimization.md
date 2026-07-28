# Closed-Loop Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement.

**Goal:** Close the remaining breakpoints: job-ify library rebuild, add frontend indicators, create smoke test.

**Architecture:** Rebuild enqueues an RQ job (following existing `admin_operations.py` pattern) tracked in Jobs page via ProgressData. Frontend shows operation badges and rebuild status. Smoke test validates full pipeline.

**Tech Stack:** Python 3.12, FastAPI, RQ, Next.js 14, TanStack Query

## Global Constraints

- Reuse existing `admin_operations.py` pattern for async operations
- No new database tables — reuse `download_jobs.progress_data` pattern or Redis keys
- `shell=True` FORBIDDEN
- Commit after each task

---

### Task 1: Backend — Async Library Rebuild RQ Job

**Files:**
- Modify: `backend/app/jobs/admin_operations.py` — add `run_library_rebuild_operation`
- Modify: `backend/app/api/admin.py` — replace sync rebuild with RQ enqueue

**Interfaces:**
- Produces: `run_library_rebuild_operation(job_id: str)` — RQ entry point
- Consumes: `rebuild_library_index()` from `app.services.admin_data`

- [ ] **Step 1: Add RQ job function to admin_operations.py**

After the existing `run_clear_operation` function, add:

```python
def run_library_rebuild_operation(job_id: str) -> dict:
    """Entry point for RQ workers — rebuild /library/ from DB."""
    return asyncio.run(_run_library_rebuild_operation(job_id))


async def _run_library_rebuild_operation(job_id: str) -> dict:
    from app.services.admin_data import rebuild_library_index
    set_operation_status(job_id, "running", "admin-rebuild",
        progress={"phase": "running", "label": "Rebuilding library index..."},
        meta={"entity": "library"})
    try:
        async with async_session() as db:
            result = await rebuild_library_index(db)
        set_operation_status(job_id, "complete", "admin-rebuild",
            progress={"phase": "complete", "label": result.get("message", "Complete")},
            result=result, meta={"entity": "library"})
        return result
    except Exception as exc:
        logger.exception("Library rebuild failed: job_id=%s", job_id)
        set_operation_status(job_id, "failed", "admin-rebuild",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "library"})
        raise
```

- [ ] **Step 2: Replace sync endpoint with RQ enqueue in admin.py**

Find `@router.post("/library/rebuild")` and replace the function body:

```python
@router.post("/library/rebuild")
async def rebuild_library(db: AsyncSession = Depends(get_db)):
    """Enqueue a library rebuild operation and return immediately."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status

    job_id = str(uuid.uuid4())
    set_operation_status(job_id, "enqueued", "admin-rebuild",
        progress={"phase": "enqueued", "label": "Library rebuild queued"},
        meta={"entity": "library"})

    Queue(name="operations", connection=get_redis()).enqueue(
        "app.jobs.admin_operations.run_library_rebuild_operation",
        job_id, job_timeout=7200)

    return {"status": "enqueued", "job_id": job_id, "message": "Library rebuild queued"}
```

- [ ] **Step 3: Verify syntax and commit**

```bash
python3 -c "import ast; ast.parse(open('backend/app/jobs/admin_operations.py').read()); ast.parse(open('backend/app/api/admin.py').read()); print('OK')"
git add backend/app/jobs/admin_operations.py backend/app/api/admin.py
git commit -m "feat: async library rebuild via RQ job"
```

---

### Task 2: Frontend — Jobs Page Operation Type Badge

**Files:**
- Modify: `admin-web/src/app/admin/jobs/page.tsx` — add operation badge
- Modify: `admin-web/src/lib/i18n.tsx` — new keys

- [ ] **Step 1: Add operation type badge in job row**

In the download job row, after StatusBadge (~line 616), insert:

```tsx
{j.operation_type && j.operation_type !== "download" && (
  <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-purple-100 text-purple-700 shrink-0">
    {t(`jobs.op_${j.operation_type}`, j.operation_type)}
  </span>
)}
```

- [ ] **Step 2: Add i18n keys**

zh: `"jobs.op_library_rebuild": "重建索引"` / `"jobs.op_admin_clear": "数据清理"`
en: `"jobs.op_library_rebuild": "Rebuild"` / `"jobs.op_admin_clear": "Cleanup"`

- [ ] **Step 3: Commit**

```bash
git add admin-web/
git commit -m "feat: add operation type badge to jobs page"
```

---

### Task 3: Frontend — Dashboard + Works Filter

**Files:**
- Modify: `admin-web/src/app/admin/page.tsx` — rebuild indicator
- Modify: `backend/app/api/system.py` — add rebuild_active count
- Modify: `admin-web/src/lib/i18n.tsx`

- [ ] **Step 1: Backend workbench — add rebuild_active count**

In `system.py` workbench_summary, in the queue section add:
```python
"rebuild_active": _count_active_rebuilds(),
```

Add helper before workbench_summary:
```python
async def _count_active_rebuilds() -> int:
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        keys = r.keys("op:*:status")
        return sum(1 for k in keys if r.get(k) == b"running")
    except: return 0
```

- [ ] **Step 2: Frontend Dashboard — rebuild indicator**

In the workbench StatCards section, add:
```tsx
{workbench.data?.queue?.rebuild_active > 0 && (
  <StatCard label={t("dashboard.rebuild_active")} value={workbench.data.queue.rebuild_active} sub={t("dashboard.rebuild_active_sub")} tone="active" />
)}
```

i18n: `"dashboard.rebuild_active": "重建中"`, `"dashboard.rebuild_active_sub": "Library 重建任务"`

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/system.py admin-web/
git commit -m "feat: dashboard rebuild indicator"
```

---

### Task 4: Smoke Test Script

**Files:**
- Create: `scripts/smoke-test.sh`

- [ ] **Step 1: Create the script**

```bash
#!/usr/bin/env bash
set -euo pipefail
G='\033[0;32m'; R='\033[0;31m'; N='\033[0m'
pass() { echo -e "${G}[PASS]${N} $1"; }
fail() { echo -e "${R}[FAIL]${N} $1"; exit 1; }

API="http://localhost:8818"
FI="/volume3/docker/auto-gallery/data/downloads/.file-index.sqlite3"

echo "=== auto-gallery Smoke Test ==="

# 1. Health
curl -sf "$API/api/v1/system/health" | grep -q '"ok"' && pass "API health" || fail "API health"

# 2. FileIndex
[ -f "$FI" ] && pass "FileIndex exists" || fail "FileIndex missing"
ENTRIES=$(sqlite3 "$FI" "SELECT COUNT(*) FROM file_index" 2>/dev/null || echo 0)
[ "$ENTRIES" -gt 0 ] && pass "FileIndex: $ENTRIES entries" || fail "FileIndex empty"

# 3. Random work metadata + thumbnail
WID=$(sqlite3 "$FI" "SELECT work_id FROM file_index WHERE file_type='metadata_json' AND import_status='done' ORDER BY RANDOM() LIMIT 1" 2>/dev/null || echo "")
if [ -n "$WID" ]; then
  ROW=$(sqlite3 "$FI" "SELECT source,creator_dir FROM file_index WHERE work_id='$WID' LIMIT 1")
  SOURCE=$(echo "$ROW" | cut -d'|' -f1)
  CREATOR=$(echo "$ROW" | cut -d'|' -f2)
  META="/volume3/docker/auto-gallery/data/library/$SOURCE/$CREATOR/$WID/metadata.json"
  [ -f "$META" ] && pass "metadata: $SOURCE/$CREATOR/$WID" || fail "metadata missing: $META"
else
  echo "  (no imported works — skip file check)"
fi

# 4. Search
curl -sf "$API/api/v1/search?q=pixiv" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'total' in d" 2>/dev/null && pass "Search API" || fail "Search API"

# 5. Workbench
curl -sf "$API/api/v1/system/workbench" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'queue' in d" 2>/dev/null && pass "Workbench API" || fail "Workbench API"

echo -e "\n=== ${G}All checks passed${N} ==="
```

- [ ] **Step 2: Make executable and test**

```bash
chmod +x scripts/smoke-test.sh
bash scripts/smoke-test.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/smoke-test.sh
git commit -m "feat: add smoke test script validating full pipeline"
```

---

### Task 5: Build and Verify

- [ ] **Step 1: Build + restart**

```bash
cd /volume3/docker/auto-gallery
docker compose build backend admin-web
docker compose up -d --force-recreate backend admin-web
bash scripts/debug.sh quick
```

- [ ] **Step 2: Run smoke test**

```bash
bash scripts/smoke-test.sh
```

- [ ] **Step 3: Final commit**

```bash
git add .
git commit -m "chore: closed-loop optimization complete"
```
