# Development Guide

## Project Structure

```text
auto-gallery/
  docker-compose.yaml
  .env.example
  README.md
  docs/
    architecture.md    architecture.zh.md
    setup.md           setup.zh.md
    development.md     development.zh.md
    providers.md       providers.zh.md
    risks.md           risks.zh.md
  backend/
    Dockerfile
    requirements.txt
    alembic.ini
    app/
      main.py
      config.py
      database.py
      models/
      schemas/
      repositories/
      services/
      api/
      providers/
      jobs/
    alembic/
      env.py
      versions/
    tests/
  admin-web/
    Dockerfile
    package.json
    tsconfig.json
    tailwind.config.ts
    src/
      app/
        admin/           # all admin pages
          creators/      # list, detail, mapping, duplicates
          subscriptions/ # list, detail
          jobs/          # download + import unified queue
          works/         # list, detail
          tags/          # tag manager
          scheduler/     # sync schedule + queue
          search/        # Meilisearch full-text search
          reference/     # Danbooru reference mapping
          sources/       # provider capability matrix
          system/        # system health dashboard
          settings/      # gallery-dl, dedup, proxy, auth-status, logs, backup, naming-templates
          data-mgmt/     # storage stats, integrity checks, danger zone
          merge-candidates/
          dedup/
          login/
      components/        # shared components
        ConfirmDialog.tsx
        DataTable.tsx
        EmptyState.tsx
        ErrorBoundary.tsx
        ErrorState.tsx
        LoadingSkeleton.tsx
        Modal.tsx
        PageHeader.tsx
        SourceBadge.tsx
        StatusBadge.tsx
        Toast.tsx
        WorkGrid.tsx
      lib/               # typed API client, i18n, auth, theme
        api.ts
        auth.tsx
        i18n.tsx
        theme.tsx
```

## Backend Development

### Stack

- Python 3.12
- FastAPI
- SQLAlchemy 2.0 (async)
- Alembic
- PostgreSQL 16
- Redis (RQ)
- Meilisearch

### Ports

| Context              | Backend | Admin Web |
|----------------------|---------|-----------|
| Host (mapped)        | 8818    | 13000     |
| Container-internal   | 8000    | 3000      |

### Running locally (without Docker for iteration)

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Requires postgres, redis, meilisearch running (via Docker)
docker compose up -d postgres redis meilisearch

# Run API (container-internal port 8000)
uvicorn app.main:app --reload --port 8000
```

### Running tests

```bash
# All tests
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 backend python -m pytest

# Single test file
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 backend python -m pytest tests/test_providers.py

# With coverage
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 backend python -m pytest --cov=app
```

For local virtualenv runs, install the same dependency set used by CI:

```bash
cd backend
source venv/bin/activate
pip install -r requirements.txt
python -m pytest
ruff check app tests
```

The test suite sets safe test defaults in `tests/conftest.py` before importing
the app, so local runs do not need production secrets.

If a previous root/container run left host-owned bytecode caches behind, remove
them before running tests as your normal user:

```bash
sudo find backend -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
```

### Python dependency lock

Backend runtime and CI install `requirements.txt`, which is generated from
`requirements.in` with `pip-tools`.

```bash
cd backend
python -m pip install pip-tools
pip-compile --resolver=backtracking --allow-unsafe --output-file requirements.txt requirements.in
```

Keep provider-sensitive packages such as `gallery-dl` pinned and run the Docker
Compose smoke test before merging dependency updates.

### Database migrations

```bash
# Create a new migration
docker compose run --rm backend alembic revision --autogenerate -m "description"

# Apply migrations
docker compose run --rm backend alembic upgrade head

# Rollback one
docker compose run --rm backend alembic downgrade -1
```

### Migration safety

- Always review `--autogenerate` output before committing.
- Watch for: drop tables/columns, constraint changes, column renames (autogenerate sees rename as drop+add).
- If a migration loses data, explicitly state the reason in the commit message.
- Test both `alembic upgrade head` and `alembic downgrade -1` before merging.

### Code conventions

- Business logic in services, never in route handlers
- Database access through repositories, never in services directly
- Pydantic schemas for all API inputs/outputs
- Provider modules have no database access
- JSONB for raw source metadata
- Idempotent import logic (re-running an import must not create duplicates)

## Provider Development

See [docs/providers.md](providers.md) for the full provider interface and implementation guide.

## Admin Web Development

### Stack

- Next.js 14 (14.2.18)
- React 18 (18.3.1)
- TypeScript (5.7.2)
- Tailwind CSS (3.4.16)
- TanStack Query (5.62.0)

### Pages

The admin web is fully built with the following pages:

| Section | Pages |
|---------|-------|
| Dashboard | System health, storage, recent activity |
| Creators | List, detail, identity mapping, duplicates |
| Subscriptions | List, detail (multi-source config) |
| Jobs | Unified download + import job queue |
| Works | Grid/list view, detail with source records and assets |
| Tags | Tag manager with search and categories |
| Scheduler | Sync schedule, queue status, scheduler config |
| Search | Meilisearch full-text search across works, creators, tags |
| Danbooru Reference | Artist search, URL batch import, Pixiv ID search |
| Sources | Provider capability matrix, URL validation |
| Settings > gallery-dl | Per-source extractor config (auth, content, rate limits) |
| Settings > Dedup | Source-level, cross-source, and perceptual hash dedup toggles |
| Settings > Proxy | HTTP/HTTPS proxy config for gallery-dl and API calls |
| Settings > Auth Status | Cookie/token health monitoring per subscription source |
| Settings > Logs | Live log viewer from in-memory ring buffer |
| Settings > Backup & Restore | Full system backup creation and restore |
| Settings > Naming Templates | File organization patterns per source |
| Settings > Download Defaults | Timeout, retries, backoff, max posts |
| Settings > Subscription Defaults | Sync interval, schedule mode, timezone |
| Data Management | Storage stats, integrity checks, cleanup, danger zone |

### Running admin-web locally

```bash
cd admin-web
npm install

# Start dev server on port 3000
npm run dev
```

The dev server proxies API requests to the backend (configured via `BACKEND_INTERNAL_URL` env var).

### Building for production

```bash
npm run typecheck
npm run build
```

### Admin-web debugging

```bash
# Check for TypeScript/build errors
npm run typecheck
npm run build

# The build output will show any type errors or compilation issues
```

### i18n (Internationalization)

The admin web supports Chinese and English. All UI strings are defined in `src/lib/i18n.tsx` with both `zh` and `en` values. Language preference is stored in localStorage and defaults to the browser's language.

When adding new UI:

1. Add keys for ALL text that appears in the UI
2. Use Chinese values that read naturally to a Chinese speaker
3. Use English values that read naturally to an English speaker
4. Never display raw i18n key IDs -- if you see a key displayed instead of its value, the key is missing from i18n.tsx

Key format: `namespace.natural_name` (e.g., `jobs.download`, `creators.title`).

### Admin web conventions

- Typed API client in `src/lib/api.ts`
- Reusable components in `src/components/`
- Pages keep logic thin; TanStack Query handles server state
- Mobile-friendly but desktop-optimized
- Dark mode support via `src/lib/theme.tsx`

## Debugging

### Viewing logs

```bash
# All services
docker compose logs -f

# Specific services
docker compose logs -f worker-download
docker compose logs -f worker-import

# Last 100 lines
docker compose logs --tail=100 backend
```

### Accessing the database

```bash
docker compose exec postgres psql -U autogallery
```

### Checking Redis queue

```bash
docker compose exec redis redis-cli -a $REDIS_PASSWORD
> KEYS *
> LLEN rq:queue:default
> LLEN rq:queue:downloads
> LLEN rq:queue:imports
> LLEN rq:queue:scheduled
```

### Testing gallery-dl manually

```bash
docker compose exec worker-download gallery-dl --version
docker compose exec worker-download gallery-dl --config /gallerydl-config/config.json "https://www.pixiv.net/artworks/123456"
```

### Rebuilding after dependency changes

```bash
docker compose build backend
docker compose up -d backend worker-download worker-import scheduler

# Or for admin-web
docker compose build admin-web
docker compose up -d admin-web
```

### Runtime verification

```bash
docker compose build backend admin-web
docker compose up -d backend admin-web worker-download worker-import scheduler
scripts/verify-runtime.sh
```

For CI-style runtime verification with the checked-in safe environment:

```bash
docker compose --env-file .env.ci config --quiet
docker compose --env-file .env.ci build backend admin-web
docker compose --env-file .env.ci up -d
COMPOSE_ENV_FILE=.env.ci scripts/verify-runtime.sh
docker compose --env-file .env.ci down -v
rm -rf data
```
