# auto-gallery Closed-Loop Optimization Design

**Date**: 2026-06-20 | **Status**: Draft

---

## 1. Context: What We've Built

21 commits, 3 phases, 15+ hours. Backend: FileIndex SQLite → Redis Stream → Per-source queues → 3-layer retry.
Frontend: status classification → batch scheduler → data management → debug toolkit.
The core download→import→search→thumbnail pipeline is closed-loop — all paths use gallery-dl config templates.

## 2. Breakpoint Audit

| Component | Status |
|-----------|--------|
| Works page → file locations | ✅ Closed (gallery-dl template) |
| Search → Meilisearch | ✅ Closed (indexed at import) |
| Media API (/thumb, /original) | ✅ Closed (relative paths) |
| Import (runner + stream) | ✅ Closed (both use template) |
| Library rebuild | ❌ Synchronous API call |
| Dashboard attention | ⚠️ No rebuild indicator |
| Works filter | ⚠️ No "missing thumbnail" filter |
| E2E verification | ❌ No smoke test |

## 3. Design

### Fix A: Library Rebuild → Async RQ Job

Replace synchronous `POST /admin/library/rebuild` with RQ enqueue. Job tracked in Jobs page via `operation_type="library_rebuild"`. Progress stored in `progress_data`.

### Fix B: Data Management → Jobs Workflow

Three-step: Clear Library → Rebuild Library (enqueues job) → Rebuild Search (existing reindex). Each links to Jobs page.

### Fix C: Frontend Additions

- Dashboard: show active rebuild count
- Works list: "no thumbnail" quick filter (`thumb_sm_path IS NULL`)
- Jobs page: operation type badge (download/import/rebuild)

### Fix D: Smoke Test Script

`scripts/smoke-test.sh` validates API→FileIndex→metadata.json→thumbnail→search→scheduler.

## 4. Files

| File | Action |
|------|--------|
| `backend/app/api/admin.py` | Modify |
| `backend/app/jobs/admin_operations.py` | Modify |
| `admin-web/src/app/admin/jobs/page.tsx` | Modify |
| `admin-web/src/app/admin/page.tsx` | Modify |
| `admin-web/src/app/admin/works/page.tsx` | Modify |
| `admin-web/src/lib/i18n.tsx` | Modify |
| `scripts/smoke-test.sh` | Create |
