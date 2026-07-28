# Runbook — auto-gallery Operations

<!-- AUTO-GENERATED: runbook — generated 2026-06-20 from docker-compose.yaml + scripts/ -->

## Deployment

### Quick Deploy (recommended)

```bash
cd /volume3/docker/auto-gallery
bash scripts/deploy.sh
```

This single command:
1. Detects source changes (backend, admin-web, docker-compose.yaml, scripts/)
2. Builds only if sources changed (handles ECR timeouts gracefully)
3. Force-recreates all app containers
4. Waits up to 90s for all containers to report healthy
5. Runs a quick health summary

### Manual Deploy

```bash
# 1. Build images
docker compose build backend admin-web

# 2. Restart all app containers
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web

# 3. Wait for healthy (up to 90s)
docker compose ps --format "table {{.Name}}\t{{.Status}}"

# 4. Verify
bash scripts/debug.sh quick
```

### Deploying Only Infrastructure Changes

If only `docker-compose.yaml` changed (no code changes):

```bash
docker compose up -d --force-recreate
bash scripts/debug.sh quick
```

## Health Check Endpoints

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/v1/system/ready` | No | Container health check (PG + Redis + Meili liveness) |
| `GET /api/v1/system/health` | No | Detailed health (services + disk) |
| `GET /api/v1/system/health/disk` | Admin | Disk usage breakdown |
| `GET /api/v1/system/workbench` | Admin | Full dashboard: queue stats, storage, proxy health |
| `GET /api/v1/system/queue-stats` | Admin | Pending/running/failed counts per queue |
| `GET /api/v1/system/logs` | Admin | Tail buffered log output |
| `GET /media/thumb/{asset_id}` | No | Thumbnail serving (used by frontend img tags) |

### Container Health Checks

| Container | Check | Interval | Timeout |
|-----------|-------|----------|---------|
| `postgres` | `pg_isready` | 5s | 5s |
| `redis` | `redis-cli ping` | 5s | 5s |
| `meilisearch` | `wget /health` | 10s | 5s |
| `backend` | `curl /api/v1/system/ready` | 30s | 15s |
| `worker-download` | Redis ping + gallery-dl exists | 30s | 10s |
| `worker-import` | Redis ping | 30s | 10s |
| `worker-import` | Redis ping | 30s | 10s |
| `scheduler` | Redis ping | 30s | 10s |
| `admin-web` | `wget /` | 15s | 5s |

## Monitoring

### Quick Status

```bash
# Container health
docker compose ps

# Resource usage
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"

# Queue lengths
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" LLEN rq:queue:downloads:pixiv
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" LLEN rq:queue:imports
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" LLEN rq:queue:imports
```

### Log Tailing

```bash
# All services
docker compose logs -f --tail=50

# Specific service
docker compose logs -f --tail=50 worker-download
docker compose logs -f --tail=50 worker-import
```

### Debug Toolkit

```bash
bash scripts/debug.sh quick     # Container health + disk + queue summary
bash scripts/debug.sh backend   # Backend logs + DB pool + API checks
bash scripts/debug.sh download  # Download worker logs + queue lengths
bash scripts/debug.sh storage   # Disk usage + FileIndex stats
bash scripts/debug.sh proxy     # Proxy connectivity + DNS checks
```

## Common Issues

### Backend memory pressure

**Symptom:** `docker stats` shows backend near memory limit, API slow to respond.

**Fix:**
```bash
# Check current usage
docker stats --no-stream | grep backend

# If >80%, restart backend to clear Python memory fragmentation
docker compose up -d --force-recreate backend
```

Current config: `mem_limit: 1024M` — if consistently near limit, increase in docker-compose.yaml.

### Download jobs stalling (proxy)

**Symptom:** Download jobs stuck in `downloading` status, logs show "stalled: no progress for 120s".

**Causes:**
- Proxy (mihomo) unreachable or misconfigured
- Source platform rate-limiting or blocking the proxy IP
- DNS resolution failure inside container

**Fix:**
```bash
# 1. Check proxy health from workbench
curl -H "Authorization: Bearer <token>" http://localhost:8818/api/v1/system/workbench | jq .proxy_health

# 2. Check proxy connectivity from inside worker
docker compose exec worker-download curl -x http://host.docker.internal:7890 -I https://www.pixiv.net --max-time 10

# 3. Check DNS
docker compose exec worker-download python3 -c "import socket; print(socket.getaddrinfo('www.pixiv.net', 443))"
```

### ECR timeout during build

**Symptom:** `docker compose build` fails with `dial tcp 198.18.0.124:443: i/o timeout`.

**Fix:** The base image is now `python:3.12.13-slim-bookworm` from Docker Hub (not ECR). If the issue recurs, use `bash scripts/deploy.sh` which handles build timeouts gracefully and falls back to the existing image.

### Stale download jobs not detected

**Symptom:** Jobs stuck in `downloading` forever, never marked stale.

**Check:** Stale detection reads Redis heartbeat keys (`task:{job_id}:heartbeat_ts`). If heartbeat publishing failed:
```bash
# Check if heartbeat keys exist
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" KEYS "task:*:heartbeat_ts"

# Force kill stuck jobs
curl -X POST -H "Authorization: Bearer <token>" http://localhost:8818/api/v1/download-jobs/kill-stuck
```

### DB connection pool exhausted

**Symptom:** API returns 500, logs show "QueuePool limit … reached".

**Fix:** Current pool is 2+2 per process (7 processes = max 28 connections). If exhausting:
```bash
# Check active connections
docker compose exec postgres psql -U autogallery -c "SELECT count(*) FROM pg_stat_activity WHERE datname='autogallery';"

# Restart all Python containers to free connections
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler
```

### Health check failures after deploy

**Symptom:** `docker compose ps` shows `unhealthy` for multiple containers.

**Fix:**
```bash
# Check backend first (dependency for admin-web)
docker compose logs --tail=30 backend

# If backend shows "database … does not exist", run migrations
docker compose exec backend alembic upgrade head

# If Redis auth fails, verify REDIS_PASSWORD in .env matches
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" ping

# Restart all
docker compose up -d --force-recreate
```

## Rollback

### Git-based rollback

```bash
# 1. Checkout previous known-good commit
git checkout <previous-commit>

# 2. Rebuild and redeploy
bash scripts/deploy.sh
```

### Image-based rollback

If a previous image tag is still available:
```bash
# Tag the previous image
docker tag auto-gallery-backend:<previous-version> auto-gallery-backend:latest

# Recreate containers
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web
```

### Configuration rollback

```bash
# Restore .env from backup
cp .env.backup .env

# Restore gallery-dl config
cp -r data/config/gallery-dl.backup/* data/config/gallery-dl/

# Restart
docker compose up -d --force-recreate
```

## Alerting & Escalation

### What to watch

| Metric | Threshold | Action |
|--------|-----------|--------|
| Backend memory | >80% (820M/1024M) | Restart backend, investigate leak |
| Download queue depth | >50 per source | Check proxy, source platform status |
| Import queue depth | >100 | Check worker-import logs and artifact backlog |
| Failed jobs (24h) | >10 | Investigate error patterns in job logs |
| Disk usage | >90% | Run cleanup, consider expansion |
| Health check failures | >3 consecutive | Check service logs, restart |

### Escalation path

1. **Check debug toolkit:** `bash scripts/debug.sh <mode>`
2. **Check service logs:** `docker compose logs --tail=100 <service>`
3. **Check workbench:** `curl localhost:8818/api/v1/system/workbench` (with auth)
4. **Restart affected service:** `docker compose up -d --force-recreate <service>`
5. **Full restart:** `bash scripts/deploy.sh`
6. **If all else fails:** Check host system resources (NAS DSM Resource Monitor), verify network connectivity, check for Docker daemon issues

### Backup verification

```bash
# Manual backup
curl -X POST -H "Authorization: Bearer <token>" http://localhost:8818/api/v1/admin/backup

# List backups
curl -H "Authorization: Bearer <token>" http://localhost:8818/api/v1/admin/backup
```

Auto-backup runs every 24 hours if enabled via Settings > Backup & Restore in the admin web.

<!-- /AUTO-GENERATED -->
