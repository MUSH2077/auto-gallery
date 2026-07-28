# auto-gallery Performance Optimization Design

**Date**: 2026-06-20 | **Status**: Draft

## Diagnosis

Backend 511.9M/512M (99.97%) — limit too low for FastAPI+SQLAlchemy+PyVips. Constant GC. Frontend 3s polling amplifies latency.

## Fixes

| # | Fix | File | Before | After |
|---|-----|------|--------|-------|
| 1 | Backend memory | docker-compose.yaml | 512M | 1024M |
| 2 | Frontend polling | jobs/page.tsx | 3s/10s | 8s/30s |
| 3 | Workbench cache | system.py | none | 30s TTL |
| 4 | Health intervals | docker-compose.yaml | 20-30s | 60s |
| 5 | DB pool shrink | database.py | 3+3 | 2+2 |

## Files

| File | Action |
|------|--------|
| `docker-compose.yaml` | mem_limit + health intervals |
| `admin-web/src/app/admin/jobs/page.tsx` | polling constants |
| `backend/app/api/system.py` | workbench cache |
| `backend/app/database.py` | connection pool |
