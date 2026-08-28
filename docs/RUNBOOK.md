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
uses a permanent **schema-forward application rollback** policy. If the database
is still at the pre-deploy revision, the old image's migrate service may run. If
the candidate revision has already been applied, the script retains that schema,
skips the old image's migrate service (whose Alembic graph may not know the
candidate revision), restores the tagged backend/web images, and brings back only
the foreground stack under the new resource ceilings. Any unexpected database
revision is refused. Heavy workers remain stopped until the failure is understood:

```bash
/volume2/docker/auto-gallery-deployments/<deployment-id>/rollback.sh
```

Use the checksummed `postgres.dump` only for verified data corruption. Ordinary
code or migration rollback should not overwrite the database from a dump.
Inspect `rollback-receipt.env` for the policy, observed revision, whether the
schema was retained, and whether old migrate ran. Every production migration
must remain backward-compatible with the previous application image. If that
image is incompatible with the retained candidate schema, restore the candidate
image and ship a forward repair; never downgrade or drop the multi-user tables.

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

## Remote discovery rollout and recovery

`worker-operations` supervises a separate `discovery` RQ child queue in the
same bounded container. The scheduler only admits due, enabled, credentialed,
healthy accounts; the discovery worker fetches pages and checkpoints the
cursor. Neither scheduler nor Redis carries credential plaintext. Inspect both
parents when diagnosing admission:

```bash
docker compose logs --tail=200 scheduler worker-operations
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" LLEN rq:queue:discovery
```

Enable stages one at a time and recreate backend, scheduler, and
worker-operations after each `.env` change:

1. `REMOTE_DISCOVERY_PRIVATE_MEMBERS_ENABLED=true`
2. `REMOTE_DISCOVERY_PIXIV_PREVIEW_ENABLED=true`
3. `REMOTE_DISCOVERY_PIXIV_AUTO_IMPORT_ENABLED=true`
4. `REMOTE_DISCOVERY_X_ENABLED=true`
5. `REMOTE_DISCOVERY_X_AUTO_IMPORT_ENABLED=true`
6. `REMOTE_DISCOVERY_BILIBILI_ENABLED=true`
7. `REMOTE_DISCOVERY_BILIBILI_AUTO_IMPORT_ENABLED=true`

Observe manual preview with dedicated test accounts before opening each auto
stage. To stop a stage, set its flag to `false` and recreate the three services.
The API, scheduler admission, worker claim, and auto-import execution all fail
closed. Do not clear tables or change account settings: encrypted accounts,
candidates, private memberships, shared repositories, works, and files remain.

Back up `REMOTE_CREDENTIAL_KEY` in the encrypted operator-secret store. It is
not part of a PostgreSQL or media archive. If the key is lost or wrong, disable
all preview/auto flags, retain the database, and reconnect each remote account
with a new key. Authentication failure caused by wrong AAD or ciphertext
tampering is intentionally unrecoverable. Online rotation is not supported;
rotation needs downtime plus an audited all-row decrypt/re-encrypt migration.
Never print or paste the key/cookies/tokens into logs, task metadata, Redis, or
incident tickets.

`X_OAUTH_REDIRECT_URI` must be the public admin frontend URL ending in
`/admin/discovery`, never the backend completion endpoint. The frontend removes
the provider's query before hydration and completes through a query-free POST.
Its own incoming-request log is suppressed for that route. The reverse proxy
still sees the first callback request, so its access-log format must record a
path-only target (`$uri` in Nginx) or explicitly redact the query for
`/admin/discovery`; do not log callback headers or bodies.

`worker-download` must have `PERSONAL_AUTH_TMP_ROOT=/run/auto-gallery-secrets`
mounted as `tmpfs` with mode `0700`. A startup failure mentioning personal
authentication tmpfs is a hard security failure: verify `docker compose config`
still shows the service-local tmpfs and do not redirect the path to
`GALLERYDL_CONFIG_ROOT`, `APP_CONFIG_ROOT`, downloads, or library storage.
Backup estimates and archives intentionally exclude `gallerydl-config/jobs`.

The remote-discovery migrations are additive. Application rollback retains the
new tables and canonical summary caches. Production operators must not run an
Alembic downgrade that drops multi-user membership, remote account, candidate,
or credential-generation data. The generated rollback records
`ROLLBACK_SCHEMA_POLICY=schema-forward`; when the candidate revision is present,
it restores only the previous backend/web images and deliberately skips their
migrate service. If the previous image cannot tolerate the retained schema,
restore the candidate image and perform a forward fix instead.

Do not downgrade past the subscription-source identity alignment after a
subscription has multiple identities for one provider. Restoring the legacy
provider-only uniqueness constraint fails rather than deleting or merging rows;
production recovery remains schema-forward.

Live provider smoke tests are opt-in and excluded from normal network-free
pytest runs. Use only dedicated test accounts in a controlled shell; the exact
opt-in is `AUTO_GALLERY_LIVE_REMOTE_DISCOVERY=explicitly-enabled` plus one of
`LIVE_PIXIV_REFRESH_TOKEN`, `LIVE_X_COOKIE`, or `LIVE_BILIBILI_SESSDATA`.
Credential values are hidden from parameter IDs and failure output.

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

## Staged offline restore

The admin Backup settings page uploads the archive in resumable chunks and runs
the non-destructive validation TaskRun. It does not modify PostgreSQL, Redis,
configuration, or library data. Continue only after the page shows **Ready for
offline host execution**, a request ID, and a host command.

Before running that command, open a host shell in the exact Compose project and
export the absolute paths from the deployment's `.env`. `HOST_RESTORE_STAGING`
and `HOST_RESTORE_RECEIPTS` must be different directories; config, downloads,
and library targets must not overlap either directory or each other.

```bash
cd /volume2/docker/auto-gallery
export PROJECT_ROOT="$PWD"
export HOST_RESTORE_STAGING=/volume1/auto-gallery/restore-staging
export HOST_RESTORE_RECEIPTS=/volume1/auto-gallery/restore-receipts
export RESTORE_STAGING_ROOT="$HOST_RESTORE_STAGING"
export RESTORE_RECEIPTS_ROOT="$HOST_RESTORE_RECEIPTS"
export HOST_CONFIG_APP=/volume1/auto-gallery/config/app
export HOST_CONFIG_GALLERYDL=/volume1/auto-gallery/config/gallery-dl
export HOST_DOWNLOADS=/volume1/auto-gallery/downloads
export HOST_LIBRARY=/volume1/auto-gallery/library

# Paste the exact read-only ready-request command shown by the admin page.
./scripts/offline-restore.py --request \
  "$HOST_RESTORE_STAGING/<request-id>/ready-request.json"
```

The command is non-interactive and takes an exclusive lock. On success it
starts foreground and background services and writes a create-once receipt at
`$HOST_RESTORE_RECEIPTS/<request-id>.json`. On any phase failure it restores the
database/config/Redis rollback point, starts foreground services only, leaves
writers stopped for diagnosis, and publishes the failing phase and rollback
status in that external receipt. The receipt's `rollback_command` is an
executable recovery point; invoke it only for that exact request after reviewing
the receipt and journal under
`$HOST_RESTORE_RECEIPTS/rollbacks/<request-id>/`.
