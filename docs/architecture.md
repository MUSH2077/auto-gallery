# Architecture

## Overview

auto-gallery is a layered Docker Compose application that downloads media from multiple sources, imports metadata into a canonical PostgreSQL database, and serves it through a FastAPI backend and Next.js admin interface.

## Layer Diagram

```
┌─────────────────────────────────────────────────┐
│  Admin Web (Next.js)                             │
│  /src/app/*  /src/components/*  TanStack Query   │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────────────┐
│  Backend (FastAPI)                               │
│  Routes → Services → Repositories → SQLAlchemy   │
│  Providers: Pixiv, Iwara, X, Danbooru, local    │
└──────┬────────────┬──────────────┬──────────────┘
       │            │              │
       │   ┌────────▼────────┐     │
       │   │  Worker (RQ)    │     │
       │   │  gallery-dl     │     │
       │   │  Import pipeline│     │
       │   └────────┬────────┘     │
       │            │              │
┌──────▼────────────▼──────────────▼──────────────┐
│  Data Layer                                      │
│  PostgreSQL ── Redis ── Meilisearch              │
│  ┌──────────┐ ┌──────┐ ┌─────────────┐          │
│  │ /downloads│ │/lib │ │ /gallerydl  │          │
│  │           │ │rary │ │ -config     │          │
│  └──────────┘ └──────┘ └─────────────┘          │
└─────────────────────────────────────────────────┘
```

## Domain Model

All models use source-agnostic naming:

```
creator ──< source_creator
creator ──< creator_link
creator ──< subscription ──< subscription_source
work ──< work_source
work ──< work_tag
work_source ──< work_source_tag
asset ──< asset_source
tag
naming_template
download_job
import_job
```

### Key relationships

- **creator**: A canonical local identity (e.g., "Artist A")
- **source_creator**: A platform-specific account (e.g., Pixiv user 123456)
- **creator_link**: URLs linking a creator to external profiles
- **work**: A canonical local work (may have multiple work_sources)
- **work_source**: A source-specific work record with original metadata
- **asset**: A local file
- **asset_source**: A source-specific file record
- **subscription**: A user's intent to follow a creator
- **subscription_source**: Per-source toggle for a subscription

### Uniqueness constraints

- `(source, source_creator_id)` on source_creators
- `(source, source_work_id)` on work_sources
- `(source, source_asset_id)` on asset_sources
- `(normalized_name)` on tags
- `(creator_id)` on subscriptions
- `(subscription_id, source)` on subscription_sources

## Provider System

Source-specific behavior is encapsulated in provider modules under `backend/app/providers/`.

Every provider implements:
- URL normalization and validation
- gallery-dl config generation (if downloadable)
- Metadata parsing (source_creator, work_source, assets, tags)

Providers never access the database. They receive raw metadata dicts and return data dicts. The service layer handles persistence.

## Data Flow

### Download Flow
```
Scheduler → create download_job (pending)
  → Worker picks up job (downloading)
    → Provider.build_gallerydl_config()
    → subprocess.run(["gallery-dl", ...], shell=False)
    → Files land in DOWNLOAD_ROOT/<job_id>/
    → Job status = downloaded
    → Enqueue import_job
```

### Import Flow
```
Worker picks up import_job
  → Read gallery-dl .info.json
  → Provider.parse_source_creator() → upsert source_creator
  → Provider.parse_work_source() → upsert work_source
  → Provider.parse_assets() → upsert asset + asset_source
  → Provider.parse_source_tags() → upsert tags + work_source_tags
  → Move files from DOWNLOAD_ROOT → LIBRARY_ROOT
  → Job status = complete
```

### Danbooru Reference Flow
```
Admin triggers Danbooru artist import
  → Fetch Danbooru artist page
  → Parse artist tags, related URLs
  → Create creator_link suggestions (status=pending)
  → Admin reviews: approve → bind to creator, reject → discard
```

## Container Paths

Application code uses only these env-var-driven paths:

| Variable | Container path | Purpose |
|---|---|---|
| `DOWNLOAD_ROOT` | `/downloads` | gallery-dl download staging |
| `LIBRARY_ROOT` | `/library` | Organized media library |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl configs, cookies, temp job configs |
| `APP_CONFIG_ROOT` | `/app-config` | Application runtime config |

NAS host paths are mapped in `docker-compose.yml` only.

## API Route Groups

```
/api/v1/system          Health, version, stats
/api/v1/sources         List sources, capabilities
/api/v1/reference       Danbooru reference operations
/api/v1/creators        Creator CRUD, source account binding
/api/v1/creator-links   Link management, approval
/api/v1/subscriptions   Subscription CRUD, source selection
/api/v1/download-jobs   Job queue, retry, cancel
/api/v1/import-jobs     Import status, errors
/api/v1/works           Work browser, work source detail
/api/v1/assets          Asset listing, metadata
/api/v1/tags            Tag management, source tag view
/api/v1/search          Meilisearch queries
/api/v1/admin           Admin settings, dedup config, merge candidates
/media                  Controlled media file serving (unversioned)
```
