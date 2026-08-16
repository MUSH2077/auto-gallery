# Runbook — auto-gallery Operations

<!-- AUTO-GENERATED: runbook — generated 2026-06-20 from docker-compose.yaml + scripts/ -->

## Deployment

### Quick Deploy (recommended)

```bash
cd /volume2/docker/auto-gallery
bash scripts/deploy.sh
```

This single command:
1. Builds immutable source-digest images serially without reading acceptance state
2. Stops background writers and creates checksummed database/config backups
3. Tags the live backend and web images for rollback
4. Runs the one-shot migration and force-recreates the protected core stack
5. Requires core services to become healthy before starting background workers
6. Verifies project-local cgroup, migration, queue and health invariants

Host memory, Swap and PSI are recorded in the rollback report but do not veto a
deployment. If the device cannot start the core containers healthily, deployment
still fails and preserves the rollback point. Use `--core-only` to deploy browsing
without workers. A formal release instead uses an already accepted candidate:

```bash
bash scripts/deploy.sh --verified /path/to/acceptance.json
```

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
| `GET /api/v1/system/ready` | No | Readiness: PostgreSQL + writable Redis required; Meilisearch is reported as degraded |
| `GET /api/v1/system/health` | No | Detailed services, disk, resource pressure, Redis, queue and worker health |
| `GET /api/v1/system/health/disk` | Admin | Disk usage breakdown |
| `GET /api/v1/system/workbench` | Admin | Full dashboard: queue stats, storage, proxy health |
| `GET /api/v1/system/queue-stats` | Admin | Pending/running/failed counts per queue |
| `GET /api/v1/system/logs` | Admin | Tail buffered log output |
| `GET /media/thumb/{asset_id}` | No | Thumbnail serving (used by frontend img tags) |

### Repair video metadata and posters

The video repair command is read-only by default. It only targets stored MP4
and WebM assets; apply mode writes derived WebP files under `LIBRARY_ROOT` and
repairs existing asset metadata without changing original media.

```bash
# Preview the number of affected video assets
docker compose run --rm backend python scripts/backfill_video_assets.py

# Apply the idempotent repair
docker compose run --rm backend python scripts/backfill_video_assets.py --apply
```

### Container Health Checks

| Container | Check | Interval | Timeout |
|-----------|-------|----------|---------|
| `postgres` | `pg_isready` | 5s | 5s |
| `redis` | Redis `SET EX` + `DEL` write probe | 30s | 5s |
| `meilisearch` | `wget /health` | 10s | 5s |
| `backend` | `curl /api/v1/system/ready` | 30s | 10s |
| `worker-download` | Supervisor heartbeat + gallery-dl exists | 30s | 10s |
| `worker-import` | Supervisor heartbeat | 30s | 10s |
| `worker-operations` | Supervisor heartbeat | 30s | 10s |
| `scheduler` | Supervisor heartbeat | 30s | 10s |
| `admin-web` | `wget /admin/login` | 15s | 5s |

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
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" LLEN rq:queue:operations
```

The system health snapshot is aggregated every 15 seconds. It includes the
adaptive controller mode, hard/soft reasons, AIMD generation and profile
grants, cgroup OOM evidence, queue activity, worker heartbeats and durable
search/Git/media/dedup outbox lag. Non-Gitllery profiles default to adaptive
`enforce`; Gitllery remains v1 shadow. Host PSI is soft AIMD feedback and all
control actions remain inside this Compose project. See `deployment-profiles.md`.

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

Current config: `mem_limit: 512M`. Do not increase it before the 24–48 hour
mixed-load algorithm-governance soak identifies the responsible stage.

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

**Fix:** The base image is now `python:3.12.13-slim-bookworm` from Docker Hub
(not ECR). `bash scripts/deploy.sh` builds serially in a bounded project
BuildKit cgroup when buildx is available. A network or build failure is treated
as a real failure; if it occurs before the freeze step, the live stack is left
unchanged. The deploy command never substitutes an unverified old image.

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

**Fix:** Backend uses an `8+4` pool; each worker/scheduler uses `1+1`, within the
PostgreSQL `max_connections=40` budget. If exhausting:
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

Every successful pre-deploy snapshot contains an executable `rollback.sh`. It
downgrades the additive migration with the candidate image, restores the tagged
backend/web images, and brings back only the foreground stack under the new
resource ceilings. Heavy workers remain stopped until the failure is understood:

```bash
/volume2/docker/auto-gallery-deployments/<deployment-id>/rollback.sh
```

Use the checksummed `postgres.dump` only for verified data corruption. Ordinary
code or migration rollback should not overwrite the database from a dump.

Gitllery remains product v1. During the segment-format rollout, keep the legacy
git-object layout read-only and leave `.gitllery.build-segment-r1` unpromoted.
After an image rollback, stop Gitllery projection if the old image cannot read
`format_id=gitllery-segment, format_revision=1`; restore the candidate image or
rebuild the shadow projection from authoritative PostgreSQL before resuming.

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
