# Startup Reliability & Responsiveness Optimization — Design

**Goal:** Fix library rebuild stuck-in-queue, eliminate cold-start 500 errors, and reduce frontend latency.

**Architecture:** Add a dedicated operations worker (new container), cache the health endpoint disk check, tune health check timeouts, and reduce frontend retry storms.

**Tech Stack:** Docker Compose, Python 3.12 FastAPI, Next.js 14 + TanStack Query 5

---

## Module 1: Operations Worker (Fix Rebuild Stuck)

### Root Cause

Library rebuild and admin clear operations are enqueued to the RQ `operations` queue via `POST /api/v1/admin/library/rebuild`. No worker listens on this queue. Jobs sit forever.

### Fix

Add `worker-operations` container (128M, 1 worker, `operations` queue). Clean orphaned `admin_operation:*` Redis keys on backend startup.

---

## Module 2: Health Endpoint Optimization

Cache disk stats in `/system/health` with 60s TTL. Expected: TTFB 303ms → ~20ms (cached).

---

## Module 3: Health Check Tuning

Backend healthcheck: timeout 15s→10s, start_period 60s→30s.

---

## Module 4: Frontend Retry Reduction

TanStack Query global `retry: 1` (was default 3). Prevents request storms on 500s.

---

## Files Summary

| File | Change |
|------|--------|
| `docker-compose.yaml` | Add `worker-operations` (128M), backend timeout/start_period tuning |
| `backend/app/main.py` | Startup: clean `admin_operation:*` Redis keys |
| `backend/app/api/system.py` | Health endpoint disk cache (60s TTL) |
| `admin-web/src/lib/api/index.ts` | QueryClient global retry=1 |

## Constraints

- No new dependencies
- Reuses existing `auto-gallery-backend:latest` image
- No API contract changes
