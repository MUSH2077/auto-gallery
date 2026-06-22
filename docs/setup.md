# Setup Guide

## Prerequisites

- Docker Engine 24+
- Docker Compose v2
- Linux host (NAS or server) on local network
- Sufficient storage for media library

## Server Ports

| Service   | Host Port | Container Port |
|-----------|-----------|----------------|
| backend   | 8818      | 8000           |
| admin-web | 13000     | 3000           |

Both ports are configurable via `BACKEND_PORT` and `ADMIN_WEB_PORT` in `.env`.

The Jobs page uses WebSocket for live progress. By default it connects to the
current site's `/api/v1/ws`; if your NAS reverse proxy does not forward
WebSocket upgrades, or you access the UI directly through the `admin-web` port,
set the public endpoint in `.env` and rebuild admin-web:

```bash
# Direct backend port
NEXT_PUBLIC_WS_URL=ws://192.168.1.100:8818/api/v1/ws

# HTTPS reverse proxy
NEXT_PUBLIC_WS_URL=wss://autogallery.example.com/api/v1/ws
```

If WebSocket is unavailable, the Jobs page automatically falls back to 3-second
polling and still updates task status. When cross-port auth fallback is needed,
admin-web requests a 30-second one-time ticket over the normal API and never
puts the long-lived login JWT in the WebSocket URL.

## Installation

### 1. Directory Structure

Create the auto-gallery directory on your NAS:

```bash
mkdir -p /volume1/auto-gallery/{downloads,library,config/{app,gallery-dl/{cookies,jobs}},docker/{postgres,redis,meilisearch}}
cd /volume1/auto-gallery
```

### 2. Configuration

```bash
scripts/generate-env.sh
```

This creates `.env` with fresh service secrets. Existing `.env` files are not overwritten unless you run `scripts/generate-env.sh --force`.

If you prefer to configure secrets manually, copy `.env.example` to `.env` and replace:

```bash
# Generate strong passwords for each service; do not keep change-me-* placeholders
POSTGRES_PASSWORD=<generate>
REDIS_PASSWORD=<generate>
MEILI_MASTER_KEY=<generate>
SECRET_KEY=<generate>

# You may keep change-me-admin for first login, or set a custom initial password
ADMIN_PASSWORD=change-me-admin
```

First login is `admin / change-me-admin`. The web UI forces a password change immediately after login. If you set a custom `ADMIN_PASSWORD` before deployment, use that custom value for first login.

If the backend logs `auto-gallery refused to start — insecure defaults detected`, confirm that Docker Compose is reading the intended `.env`, the service secrets above are no longer `change-me-*`, and the backend image has been rebuilt after updating the code.

Set your timezone:

```bash
# e.g. Asia/Shanghai, America/New_York, UTC
TIMEZONE=Asia/Shanghai
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

### 3. Start All Services

```bash
docker compose up -d
```

This starts all services: postgres, redis, meilisearch, backend, worker-download (per-source queues), worker-import (the single durable RQ import pipeline), worker-operations, scheduler, and admin-web. The backend automatically runs database migrations on startup, so no separate migration step is needed.

Wait for health checks:

```bash
docker compose ps
# All services should show "healthy"
```

If you need to start only the infrastructure services for local development:

```bash
docker compose up -d postgres redis meilisearch
```

**Tip:** For rapid redeploys after code changes, use `bash scripts/deploy.sh` — it detects changed sources, rebuilds only when needed, and waits for all containers to report healthy.

### 4. Verify

```bash
# Quick health summary (container status, disk, queue depth)
bash scripts/debug.sh quick

# Or manually:
curl http://localhost:8818/api/v1/system/health

# Expected response:
# {"status":"ok","services":{"postgres":"up","redis":"up","meilisearch":"up"}}
```

Admin web is available at `http://<host-ip>:13000`.

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

### Per-Source Configuration via Admin Web

Each source provider (Pixiv, X/Twitter, Iwara, Danbooru, Pinterest, LOFTER, Weibo, Bilibili) can be configured through the admin web UI at **Settings > gallery-dl Config**. This includes:

- Authentication (cookies, refresh tokens, API keys, username/password)
- Content filters (artworks, bookmarks, favorites, tweets, likes)
- Tag language preferences
- Ugoira format (ZIP or GIF)
- Directory and filename patterns
- Rate limiting (sleep between requests)
- Max posts per download
- Video quality preferences
- Auto-enable on import (per source)

Changes are saved to `config.json` automatically. On first startup, auto-gallery creates this file and fills missing file organization defaults without overwriting existing custom patterns. The admin web UI is the recommended way to configure gallery-dl extractors; manual editing of `config.json` is also supported as a fallback.

### File Organization

File organization is configured in **Settings > gallery-dl Config > File Organization** and saved to `GALLERYDL_CONFIG_ROOT/config.json`. Use gallery-dl's template syntax, for example `pixiv/{user[account]}/{id}`. If files are organized unexpectedly on NAS, check the matching `extractor.<source>.directory` and `extractor.<source>.filename` values in `data/config/gallery-dl/config.json`.

## Backup and Restore

The system includes a backup and restore feature accessible via **Settings > Backup & Restore** in the admin web. Backups include:

- PostgreSQL database (creators, subscriptions, works, tags, settings, job history)
- gallery-dl configuration (extractor settings, cookies, auth tokens)
- Application configuration
- Download archives (archive-*.sqlite3, for preventing duplicate downloads)

Backups can be created manually and downloaded for off-site storage. Automatic backups run every 24 hours. The restore function uploads a backup file and replaces the current system state.

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
