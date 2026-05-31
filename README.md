# auto-gallery

[中文版本](README.zh.md)

NAS-hosted multi-source media archive and gallery management system.

auto-gallery downloads, imports, indexes, and manages creator-based media libraries from multiple sources. It runs entirely through Docker Compose on a NAS or any Linux host.

## Architecture

```
Sources (Pixiv, X/Twitter, Iwara, Danbooru, Weibo, Bilibili, Pinterest, Lofter, local, manual)
  → gallery-dl (worker only)
    → DOWNLOAD_ROOT per-work directories
      → Import pipeline (metadata parsing, hashing, tagging, thumbnails)
        → LIBRARY_ROOT organized metadata + thumbnails
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

# 3. Start all services
docker compose up -d

# 4. Run migrations (first time only)
docker compose exec backend alembic upgrade head

# 5. Verify
curl http://localhost:8818/api/v1/system/health
```

Admin web at `http://<host>:13000`.

For detailed setup instructions, see [docs/setup.md](docs/setup.md).

## Services

| Service | Role |
|---|---|
| `backend` | FastAPI REST API (port 8818) |
| `worker` | Background job executor (gallery-dl downloads, import pipeline) |
| `scheduler` | Subscription sync scheduler (interval or fixed-time, per-deployment timezone) |
| `postgres` | Primary database |
| `redis` | RQ job queue + cache |
| `meilisearch` | Full-text search engine |
| `admin-web` | Next.js admin interface (port 13000) |

`backend`, `worker`, and `scheduler` share the same Docker image with different entrypoints.

## Supported Sources

| Source | Download | Status |
|---|---|---|
| Pixiv | gallery-dl | Fully supported (cookie + refresh token auth) |
| X/Twitter | gallery-dl | Downloadable (timeline, media, likes; cookie auth) |
| Iwara | gallery-dl | Downloadable (videos, images; user/pass or cookie auth) |
| Danbooru | gallery-dl | Downloadable (posts by tag search; API key or user/pass auth) |
| Weibo | gallery-dl | Downloadable (user timeline, status; optional cookie auth) |
| Bilibili | gallery-dl | Downloadable (user articles, favorites; no auth required) |
| Pinterest | gallery-dl | Downloadable (boards, pins; public API, no auth) |
| Lofter | gallery-dl | Downloadable (blog posts; no auth required) |
| Local folder | Direct import | Planned |
| Manual upload | Admin web | Planned |

All registered providers support gallery-dl download. Per-source `auto_enable_on_import` configurable in gallery-dl settings.

## Admin Web Features

- **Dashboard** — system health, service status, storage overview
- **Creators** — CRUD, source account binding, Danbooru identity mapping, merge duplicates, contribution activity grid with work links
- **Subscriptions** — per-creator with per-source enable/disable toggles
- **Jobs** — download & import queue management, batch pause/resume/retry/delete, inline import job view
- **Works** — browser with source filter, sort, batch delete/tag, work detail with source metadata, tags, assets
- **Tags** — tag management, alias, merge, category display
- **Scheduler** — unified sync + download config (schedule mode, interval, timezone, timeout, retries, AI skip), queue stats, subscription sync table
- **Data Management** — storage breakdown by source & creator top-20, integrity check (orphaned files, missing thumbnails, dead links), cleanup tools, backup & restore, danger zone
- **Settings** — gallery-dl per-source extractor config with connection test, dedup settings, proxy config with connectivity test, auth status, naming templates, logs, language toggle, search reindex

## Key Design Decisions

- **Generic domain models** — `creator`, `work`, `asset`, `tag` (not Pixiv-specific)
- **Cross-source creator identity** — one creator, many platform accounts with confidence scores
- **Multi-source subscriptions** — subscribe to a creator, choose which sources to sync per-subscription
- **Deduplication defaults to OFF** — calculate hashes, create candidates, admin decides
- **gallery-dl in worker only** — API never shells out; safe `shell=False` subprocess execution
- **Container paths only in code** — NAS host paths only in docker-compose.yaml
- **JWT authentication** — admin login with access/refresh tokens, auto-refresh
- **Timezone-aware scheduling** — configurable per-deployment, fixed-time or interval sync

## Development

See [docs/development.md](docs/development.md).

## Constraints

This project follows strict architecture constraints. See `.claude/constraints/` for details. The most critical:

1. No Pixiv-specific model names in core code
2. Danbooru serves as both a download source and a reference provider for creator identity
3. Deduplication is opt-in, never auto-deletes
4. `shell=False` for all subprocess calls
5. No NAS host paths in application code
6. Admin APIs require JWT authentication
7. No public internet exposure in v1

## License

TBD
