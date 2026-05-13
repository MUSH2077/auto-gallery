# NAS Deployment Skill

Guidance for deploying and configuring auto-gallery on a NAS host. Read `.claude/constraints/docker.md` and `.claude/constraints/security.md` before working on deployment.

## Host requirements

- Docker Engine + Docker Compose v2
- Sufficient storage for media library (separate volume recommended)
- NAS LAN access only (no public internet exposure in v1)

## Directory layout on NAS host

```
/volume1/auto-gallery/
  docker-compose.yml
  .env
  downloads/           # mapped to DOWNLOAD_ROOT
  library/             # mapped to LIBRARY_ROOT
  config/
    app/               # mapped to APP_CONFIG_ROOT
    gallery-dl/        # mapped to GALLERYDL_CONFIG_ROOT
      config.json      # base gallery-dl config
      cookies/         # auth cookies per source
      jobs/            # temp per-job configs (auto-cleaned)
  docker/
    postgres/          # PG data volume
    redis/             # Redis data volume
    meilisearch/       # Meilisearch data volume
```

## .env file (on NAS host)

```bash
# Database
POSTGRES_DB=autogallery
POSTGRES_USER=autogallery
POSTGRES_PASSWORD=<generated>

# Redis
REDIS_PASSWORD=<generated>

# Meilisearch
MEILI_MASTER_KEY=<generated>

# App
SECRET_KEY=<generated>
ADMIN_PASSWORD=<generated>

# Paths (container-side, do not change)
DOWNLOAD_ROOT=/downloads
LIBRARY_ROOT=/library
GALLERYDL_CONFIG_ROOT=/gallerydl-config
APP_CONFIG_ROOT=/app-config
```

## docker-compose.yml structure

Services communicate on an internal `auto-gallery` network. Only `backend` (port 8000) and `admin-web` (port 3000) expose ports on the NAS LAN. `postgres`, `redis`, `meilisearch` are internal only.

## volumes vs bind mounts

- `downloads/`, `library/`, `config/` → bind mounts (NAS host paths)
- `postgres/`, `redis/`, `meilisearch/` → named volumes or bind mounts (user preference)

## First-run setup

1. Copy `.env.example` to `.env`, fill in generated passwords
2. `docker compose up -d postgres redis meilisearch`
3. Wait for health checks
4. `docker compose run --rm backend alembic upgrade head`
5. `docker compose up -d`
6. Access admin-web at `http://<nas-ip>:3000`

## Backup considerations

- Database: `docker compose exec postgres pg_dump` on a cron
- Media library: use NAS snapshot/backup tools, not application-level
- Config: git-track the `config/` directory

## Development vs NAS

For local development, the same `docker-compose.yml` works with different `.env` paths:
```bash
DOWNLOAD_ROOT=./data/downloads
LIBRARY_ROOT=./data/library
GALLERYDL_CONFIG_ROOT=./data/config/gallery-dl
APP_CONFIG_ROOT=./data/config/app
```
