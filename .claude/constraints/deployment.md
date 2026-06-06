# Deployment Constraint

## Rule

Deploying backend code changes requires restarting ALL containers that share the backend image. Never assume that restarting `backend` alone is sufficient.

## Image sharing

The following services share the `auto-gallery-backend` Docker image:

| Service | Dockerfile | Queue | Runs what |
|---------|-----------|-------|-----------|
| `backend` | `backend/Dockerfile` | — | FastAPI API server |
| `worker` | same image | `default` | `rq worker` — import jobs, manual download jobs |
| `scheduler` | same image | `scheduled` | `rq worker --with-scheduler` — auto-sync scans, download jobs |

**Any code change under `backend/` potentially affects all three services.**

## Correct deploy command

```bash
# Build + deploy all services that share the backend image
docker compose build backend
docker compose up -d --force-recreate backend worker scheduler
```

Or for frontend changes:
```bash
docker compose build admin-web
docker compose up -d --force-recreate admin-web
```

## Verification after deploy

### 1. Check container creation timestamps are recent
```bash
docker inspect auto-gallery-backend-1 auto-gallery-worker-1 auto-gallery-scheduler-1 \
  --format '{{.Name}} created {{.Created}}'
```

### 2. Verify each container has the new code
```bash
# Pick a unique string from your changes
docker compose exec worker grep '<your_new_function>' /app/app/jobs/download.py
docker compose exec scheduler grep '<your_new_function>' /app/app/jobs/download.py
```

### 3. Check all services started successfully
```bash
docker compose logs backend worker scheduler --tail 5
# Look for: "Application startup complete", "Listening on...", "Cleaning registries"
```

### 4. Verify the worker and scheduler are listening on correct queues
```bash
docker compose logs worker --tail 3 | grep "Listening on"
# Should show: "Listening on default..."
docker compose logs scheduler --tail 3 | grep "Listening on"
# Should show: "Listening on scheduled..."
```

## When Docker registry is unavailable

If `docker compose build` fails due to base image pull issues:

1. Build using cached base image: `docker compose build --pull=false backend`
2. If that also fails, copy files directly and restart:
   ```bash
   docker cp backend/app/jobs/download.py auto-gallery-worker-1:/app/app/jobs/download.py
   docker cp backend/app/jobs/download.py auto-gallery-scheduler-1:/app/app/jobs/download.py
   docker compose restart worker scheduler
   ```
3. **Remember**: `docker compose restart` keeps the container but reloads the process. `docker compose up -d --force-recreate` creates a NEW container from the image (and loses any `docker cp` changes).

## Queue routing

| Queue | Listener | Job types |
|-------|----------|-----------|
| `default` | `worker` | Import jobs, manual retry jobs |
| `scheduled` | `scheduler` | Auto-sync scans, download jobs from auto-sync |

When enqueuing jobs, ensure the correct queue is targeted:
- Download jobs created by auto-sync → `Queue(name="scheduled")` 
- Import jobs created by download runner → `Queue()` (default)
- Manual retry from admin → `Queue()` (default)

## Anti-patterns

- ❌ `docker compose up -d --force-recreate backend` — worker/scheduler NOT restarted
- ❌ Assuming `docker compose restart` picks up new image — it doesn't, it reuses the same container
- ❌ Forgetting to restart scheduler after fixing subscription_sync.py
- ❌ `docker cp` to a container that gets recreated by a subsequent `docker compose up -d`
