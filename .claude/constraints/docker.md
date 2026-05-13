# Docker Constraint

## Rule

The project runs exclusively through Docker Compose. No service runs on bare metal. NAS host paths must never be hard-coded in application code.

## Required services

| Service | Purpose |
|---|---|
| `postgres` | Primary database |
| `redis` | Queue backend + cache |
| `meilisearch` | Full-text search engine |
| `backend` | FastAPI API server |
| `worker` | Background job executor (gallery-dl, imports) |
| `scheduler` | Subscription scheduler |
| `admin-web` | Next.js admin interface |

`backend`, `worker`, and `scheduler` share the same Docker image with different entrypoints/commands.

## Container path variables (application code)

Application code must reference only these environment variables for paths:

- `DOWNLOAD_ROOT` → `/downloads` (temp download staging)
- `LIBRARY_ROOT` → `/library` (organized media library)
- `GALLERYDL_CONFIG_ROOT` → `/gallerydl-config` (gallery-dl configs, cookies, temp job configs)
- `APP_CONFIG_ROOT` → `/app-config` (application runtime config)

## Docker Compose volume mapping (example)

The compose file maps these to NAS host paths. Application code never sees these:

```
/volume1/auto-gallery/downloads        → /downloads
/volume1/auto-gallery/library          → /library
/volume1/auto-gallery/config/gallery-dl → /gallerydl-config
/volume1/auto-gallery/config/app       → /app-config
/volume1/auto-gallery/docker/postgres  → /var/lib/postgresql/data
/volume1/auto-gallery/docker/redis     → /data
/volume1/auto-gallery/docker/meilisearch → /meili_data
```

## gallery-dl runtime

- gallery-dl must be included in the backend Docker image
- Only the `worker` service may execute gallery-dl
- `backend` API must never shell out to gallery-dl
- `scheduler` enqueues jobs; it does not execute gallery-dl directly

## Why

Hard-coding `/volume1` or any NAS-specific path in Python/JS makes the project impossible to run on different NAS models, in CI, or in local development. Container paths with env-var overrides make the system portable across any host that runs Docker.
