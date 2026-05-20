# Setup Guide

## Prerequisites

- Docker Engine 24+
- Docker Compose v2
- Linux host (NAS or server) on local network
- Sufficient storage for media library

## Installation

### 1. Directory Structure

Create the auto-gallery directory on your NAS:

```bash
mkdir -p /volume1/auto-gallery/{downloads,library,config/{app,gallery-dl/{cookies,jobs}},docker/{postgres,redis,meilisearch}}
cd /volume1/auto-gallery
```

### 2. Configuration

```bash
cp .env.example .env
```

Edit `.env` and set:

```bash
# Generate strong passwords for each service
POSTGRES_PASSWORD=<generate>
REDIS_PASSWORD=<generate>
MEILI_MASTER_KEY=<generate>
SECRET_KEY=<generate>
ADMIN_PASSWORD=<generate>
```

For local development, use the example dev paths already in `.env.example`. For NAS deployment, set the host paths:

```bash
# NAS host paths (in .env, referenced by docker-compose.yaml)
HOST_DOWNLOADS=/volume1/auto-gallery/downloads
HOST_LIBRARY=/volume1/auto-gallery/library
HOST_CONFIG_APP=/volume1/auto-gallery/config/app
HOST_CONFIG_GALLERYDL=/volume1/auto-gallery/config/gallery-dl
HOST_POSTGRES=/volume1/auto-gallery/docker/postgres
HOST_REDIS=/volume1/auto-gallery/docker/redis
HOST_MEILISEARCH=/volume1/auto-gallery/docker/meilisearch
```

### 3. Start Infrastructure

```bash
docker compose up -d postgres redis meilisearch
```

Wait for health checks:

```bash
docker compose ps
# All three should show "healthy"
```

### 4. Database Migrations

```bash
docker compose run --rm backend alembic upgrade head
```

### 5. Start Application

```bash
docker compose up -d
```

### 6. Verify

```bash
# Health check
curl http://localhost:8000/api/system/health

# Expected response:
# {"status":"ok","services":{"postgres":"up","redis":"up","meilisearch":"up"}}
```

Admin web is available at `http://<host-ip>:3000`.

## gallery-dl Configuration

### Pixiv Authentication

1. Log into Pixiv in your browser
2. Export cookies using a browser extension (e.g., "Export Cookies")
3. Place the cookies file at `config/gallery-dl/cookies/pixiv.txt`
4. The base gallery-dl config at `config/gallery-dl/config.json` should reference it:

```json
{
  "extractor": {
    "pixiv": {
      "cookies": "/gallerydl-config/cookies/pixiv.txt"
    }
  }
}
```

### Naming Templates

Naming templates control how files are organized in `LIBRARY_ROOT`. They use gallery-dl's template syntax. Default template is stored in the database (`naming_template` table) and can be edited via admin web.

## Development Setup

For local development (no NAS):

```bash
# Use the default .env.example paths (local directories)
cp .env.example .env

# Create local data directories
mkdir -p data/{downloads,library,config/{app,gallery-dl/{cookies,jobs}}}

# Start services
docker compose up -d
```

## Troubleshooting

### gallery-dl not found in worker
Ensure the backend image has gallery-dl installed. Check `backend/requirements.txt` includes `gallery-dl`.

### Database connection refused
PostgreSQL may not be ready when backend starts. Docker Compose `depends_on` with `condition: service_healthy` handles this.

### Permission denied on volumes
Ensure the Docker user (usually uid 1000) has write access to the host directories. On Synology NAS, you may need to set permissions via the DSM File Station.

### Meilisearch master key mismatch
The `MEILI_MASTER_KEY` in `.env` must match the `MEILI_MASTER_KEY` used by the backend. If you change it after Meilisearch has started, delete the `docker/meilisearch/` data directory and restart.
