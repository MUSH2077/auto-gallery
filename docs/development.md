# Development Guide

## Project Structure

```
auto-gallery/
  docker-compose.yaml
  .env.example
  README.md
  docs/
    architecture.md
    setup.md
    development.md
    providers.md
    risks.md
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
  admin-web/          # future
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

### Running locally (without Docker for iteration)

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Requires postgres, redis, meilisearch running (via Docker)
docker compose up -d postgres redis meilisearch

# Run API
uvicorn app.main:app --reload --port 8000
```

### Running tests

```bash
# All tests
docker compose run --rm backend pytest

# Single test file
docker compose run --rm backend pytest tests/test_providers.py

# With coverage
docker compose run --rm backend pytest --cov=app
```

### Database migrations

```bash
# Create a new migration
docker compose run --rm backend alembic revision --autogenerate -m "description"

# Apply migrations
docker compose run --rm backend alembic upgrade head

# Rollback one
docker compose run --rm backend alembic downgrade -1
```

### Code conventions

- Business logic in services, never in route handlers
- Database access through repositories, never in services directly
- Pydantic schemas for all API inputs/outputs
- Provider modules have no database access
- JSONB for raw source metadata
- Idempotent import logic (re-running an import must not create duplicates)

## Provider Development

See [docs/providers.md](providers.md) for the full provider interface and implementation guide.

## Admin Web Development (future)

### Stack
- Next.js 14+
- React 18+
- TypeScript
- Tailwind CSS
- TanStack Query

### Conventions
- Typed API client (generated or hand-written)
- Reusable components in `src/components/`
- Pages keep logic thin; TanStack Query handles server state
- Mobile-friendly but desktop-optimized

## Debugging

### Viewing logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f worker

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
```

### Testing gallery-dl manually

```bash
docker compose exec worker gallery-dl --version
docker compose exec worker gallery-dl --config /gallerydl-config/config.json "https://www.pixiv.net/en/artworks/123456"
```

### Rebuilding after dependency changes

```bash
docker compose build backend
docker compose up -d backend worker scheduler
```
