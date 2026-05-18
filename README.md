# auto-gallery

[中文版本](README.zh.md)

NAS-hosted multi-source media archive and gallery management system.

auto-gallery downloads, imports, indexes, and manages creator-based media libraries from multiple sources. It runs entirely through Docker Compose on a NAS or any Linux host.

## Architecture

```
Sources (Pixiv, Iwara, X, local, manual, Danbooru ref)
  → gallery-dl (worker only)
    → DOWNLOAD_ROOT staging
      → Import pipeline (metadata parsing, hashing, tagging)
        → LIBRARY_ROOT organized media
          → PostgreSQL (canonical models)
          → Meilisearch (full-text search)
          → Admin Web (Next.js)
```

Core principle: all domain models are source-agnostic. Provider modules encapsulate source-specific behavior. A single canonical `creator` maps to multiple `source_creator` accounts across platforms.

For full architecture details, see [docs/architecture.md](docs/architecture.md).

## Quick Start

```bash
# 1. Clone
git clone <repo-url> auto-gallery && cd auto-gallery

# 2. Configure
cp .env.example .env
# Edit .env — set passwords and host paths

# 3. Start infrastructure
docker compose up -d postgres redis meilisearch

# 4. Run migrations
docker compose run --rm backend alembic upgrade head

# 5. Start everything
docker compose up -d

# 6. Verify
curl http://localhost:8000/api/system/health
```

Admin web at `http://<host>:3000`.

For detailed setup instructions, see [docs/setup.md](docs/setup.md).

## Services

| Service | Role |
|---|---|
| `backend` | FastAPI REST API |
| `worker` | Background job executor (gallery-dl, imports) |
| `scheduler` | Subscription scheduler |
| `postgres` | Primary database |
| `redis` | Job queue + cache |
| `meilisearch` | Full-text search engine |
| `admin-web` | Next.js admin interface |

`backend`, `worker`, and `scheduler` share the same Docker image with different entrypoints.

## Supported Sources

| Source | Download | Status |
|---|---|---|
| Pixiv | gallery-dl | Fully supported |
| Iwara | gallery-dl >= 1.32 | Downloadable (videos, images, playlists) |
| X/Twitter | gallery-dl | Downloadable (timeline, media, likes) |
| Danbooru | Reference only | Artist identity mapping |
| Local folder | Direct import | Planned |
| Manual upload | Admin web | Planned |

## Key Design Decisions

- **Generic domain models** — `creator`, `work`, `asset`, `tag` (not Pixiv-specific)
- **Cross-source creator identity** — one creator, many platform accounts
- **Multi-source subscriptions** — subscribe to a creator, choose which sources to sync
- **Deduplication defaults to OFF** — calculate hashes, create candidates, admin decides
- **gallery-dl in worker only** — API never shells out; safe subprocess execution
- **Container paths only in code** — NAS host paths only in docker-compose.yml

## Development

See [docs/development.md](docs/development.md).

## Constraints

This project follows strict architecture constraints. See `.claude/constraints/` for details. The most critical:

1. No Pixiv-specific model names in core code
2. Danbooru is a reference provider, not a download source (current phase)
3. Deduplication is opt-in, never auto-deletes
4. `shell=False` for all subprocess calls
5. No NAS host paths in application code
6. Admin APIs require authentication
7. No public internet exposure in v1

## License

TBD
