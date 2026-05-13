# Technology Stack

Non-negotiable technology choices for this project. All implementation must use these.

## Backend

| Component | Choice | Version | Notes |
|---|---|---|---|
| Language | Python | 3.12 | |
| Web framework | FastAPI | latest stable | Async throughout |
| ORM | SQLAlchemy | 2.0+ | Async via asyncpg driver |
| Migrations | Alembic | latest | Must review autogenerate output before committing |
| Validation | Pydantic | v2 | Built into FastAPI |
| Database | PostgreSQL | 16 | Via Docker |
| Queue | RQ | latest | Uses Redis as backend |
| Cache | Redis | 7 | Via Docker |
| Search | Meilisearch | latest | Via Docker, admin-triggered re-indexing for v1 |
| Image processing | pyvips | latest | Preferred over Pillow for large images |
| HTTP client | httpx | latest | Async, for any outbound HTTP |
| Background jobs | RQ worker | latest | Runs in `worker` service |

## Backend (Runtime Tools)

| Component | Choice | Notes |
|---|---|---|
| Downloader | gallery-dl | Pinned version in requirements.txt; smoke-tested before upgrade |
| Video processing | ffmpeg | Installed in Docker image for thumbnail generation |

## Admin Web

| Component | Choice | Version | Notes |
|---|---|---|---|
| Framework | Next.js | 14+ | App Router |
| Language | TypeScript | 5+ | Strict mode |
| UI | React | 18+ | |
| Styling | Tailwind CSS | latest | |
| Server state | TanStack Query | latest | |
| API client | Typed fetch wrapper | | Hand-written or generated, not both |

## Deployment

| Component | Choice | Notes |
|---|---|---|
| Container runtime | Docker Compose v2 | |
| Base image | python:3.12-slim | |
| Auth mode (v1) | Simple API key | Admin routes only; JWT deferred to multi-user phase |

## Explicitly Excluded

These may be reconsidered later but are NOT in current scope:

| Excluded | Reason |
|---|---|
| Celery | RQ chosen for v1 simplicity |
| Pillow (primary) | pyvips preferred for performance |
| SQLite | PostgreSQL only |
| Serverless / k8s | Docker Compose on NAS only |
| OAuth / JWT (v1) | Simple API key for initial admin auth |
| gRPC | REST only |
| WebSockets (v1) | REST polling for job status in v1 |

## Version Pinning Policy

- All Python packages pinned with exact versions in `requirements.txt`
- gallery-dl: pin known-good version, require smoke test before bump
- Base Docker image: pin minor version (`python:3.12.X-slim`)
- Meilisearch, PostgreSQL, Redis: pin major version in `docker-compose.yml`
