# Startup Reliability & Responsiveness Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Fix library rebuild stuck-in-queue, eliminate cold-start 500 errors, reduce frontend latency.

**Architecture:** Add operations worker container, cache health endpoint disk check (60s TTL), tune healthcheck timeouts, reduce TanStack Query retry count.

**Tech Stack:** Docker Compose, Python 3.12 FastAPI, Next.js 14 + TanStack Query 5

## Global Constraints

- No new dependencies — reuses `auto-gallery-backend:latest` image
- No API contract changes
- Backward-compatible

---

### Task 1: Add worker-operations container

**Files:** `docker-compose.yaml`, `scripts/deploy.sh`

Insert after `worker-import` service:

```yaml
  worker-operations:
    image: auto-gallery-backend:latest
    pull_policy: never
    command: python worker_entrypoint.py operations 1
    environment:
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-autogallery}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-autogallery}
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      MEILI_URL: http://meilisearch:7700
      MEILI_MASTER_KEY: ${MEILI_MASTER_KEY}
      SECRET_KEY: ${SECRET_KEY}
      ADMIN_PASSWORD: ${ADMIN_PASSWORD}
      DOWNLOAD_ROOT: /downloads
      LIBRARY_ROOT: /library
      GALLERYDL_CONFIG_ROOT: /gallerydl-config
      APP_CONFIG_ROOT: /app-config
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      TIMEZONE: ${TIMEZONE:-UTC}
    volumes:
      - ${HOST_DOWNLOADS:-./data/downloads}:/downloads
      - ${HOST_LIBRARY:-./data/library}:/library
      - ${HOST_CONFIG_GALLERYDL:-./data/config/gallery-dl}:/gallerydl-config
      - ${HOST_CONFIG_APP:-./data/config/app}:/app-config
    healthcheck:
      test: ["CMD", "python3", "-c", "import os, redis; assert redis.from_url(os.environ['REDIS_URL']).ping()"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    mem_limit: 128M
```

Also add `worker-operations` to deploy.sh restart list.

- [ ] Commit: `feat: add worker-operations container for admin operations queue`

---

### Task 2: Clean orphaned Redis keys on startup

**Files:** `backend/app/main.py`

Add startup event:

```python
@app.on_event("startup")
async def cleanup_orphaned_operations():
    try:
        from app.services.redis_client import get_redis
        r = get_redis()
        keys = r.keys("admin_operation:*")
        if keys:
            r.delete(*keys)
    except Exception:
        pass
```

Merge with existing startup event if present.

- [ ] Commit: `fix: clean orphaned admin_operation Redis keys on startup`

---

### Task 3: Cache health endpoint disk check

**Files:** `backend/app/api/system.py`

Add module-level cache before `storage_stats()`:

```python
_disk_cache: dict | None = None
_disk_cache_ts: float = 0.0
_DISK_CACHE_TTL = 60.0
```

In `storage_stats()`, wrap the existing logic with cache check:

```python
now_mono = time.monotonic()
if _disk_cache is not None and (now_mono - _disk_cache_ts) < _DISK_CACHE_TTL:
    return _disk_cache
# ... existing logic ...
_disk_cache = result; _disk_cache_ts = now_mono; return result
```

- [ ] Commit: `perf: cache disk stats in health endpoint (60s TTL)`

---

### Task 4: Tune health check timeouts

**Files:** `docker-compose.yaml`

Backend healthcheck: timeout 15s→10s, start_period 60s→30s.

- [ ] Commit: `perf: reduce backend healthcheck timeout and start_period`

---

### Task 5: Reduce frontend retry count

**Files:** `admin-web/src/lib/api/index.ts` (or layout where QueryClient is created)

```typescript
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 5000 } },
});
```

- [ ] Commit: `perf: reduce TanStack Query global retry to 1`

---

### Task 6: Deploy + Verify

```bash
docker compose build backend admin-web
docker compose up -d --force-recreate backend admin-web worker-operations
```

Verify: worker-operations healthy, rebuild picked up, health <50ms cached, no 500s.

- [ ] Commit: `feat: deploy and verify startup reliability optimization`
