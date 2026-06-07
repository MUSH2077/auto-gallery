# Architecture

## Overview

auto-gallery is a layered Docker Compose application that downloads media from multiple sources, imports metadata into a canonical PostgreSQL database, and serves it through a FastAPI backend and Next.js admin interface.

## Layer Diagram

```
┌─────────────────────────────────────────────────┐
│  Admin Web (Next.js 14)                          │
│  TypeScript · Tailwind CSS · TanStack Query      │
│  Port 13000 (host) ← 3000 (container)            │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────────────┐
│  Backend (FastAPI)                               │
│  Routes → Services → Repositories → SQLAlchemy   │
│  Providers: Pixiv, Iwara, X, Danbooru,          │
│             Pinterest, LOFTER, local, manual     │
│  Port 8818 (host) ← 8000 (container)             │
│  JWT auth (access + refresh tokens)              │
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

All models use source-agnostic naming. Shared data vs user-scoped data:

```
(shared across users)
creator ──< source_creator
creator ──< creator_link
work ──< work_source
work ──< work_tag
work_source ──< work_source_tag
asset ──< asset_source
tag
naming_template

(user-scoped)
user ──< subscription ──< subscription_source
user ──< album ──< album_work ── work
user ──< download_job
import_job (inherits scope via subscription)
```

### Key relationships

- **user**: A user account (admin or regular user). Added Phase 6+.
- **creator**: A canonical local identity (e.g., "Artist A") — shared across all users
- **source_creator**: A platform-specific account (e.g., Pixiv user 123456)
- **creator_link**: URLs linking a creator to external profiles
- **work**: A canonical local work (may have multiple work_sources)
- **work_source**: A source-specific work record with original metadata
- **asset**: A local file
- **asset_source**: A source-specific file record
- **subscription**: A user's intent to follow a creator
- **subscription_source**: Per-source toggle for a subscription
- **album**: A user-created collection of works (Phase 6+)
- **album_work**: Many-to-many join between album and work

### Uniqueness constraints

- `(source, source_creator_id)` on source_creators
- `(source, source_work_id)` on work_sources
- `(source, source_asset_id)` on asset_sources
- `(normalized_name)` on tags
- `(user_id, creator_id)` on subscriptions (multiple users may subscribe to same creator)
- `(subscription_id, source)` on subscription_sources
- `(username)` on users
- `(album_id, work_id)` on album_works

## Provider System

Source-specific behavior is encapsulated in provider modules under `backend/app/providers/`.

Every provider implements:
- URL normalization and validation
- gallery-dl config generation (if downloadable)
- Metadata parsing (source_creator, work_source, assets, tags)

Providers never access the database. They receive raw metadata dicts and return data dicts. The service layer handles persistence.

## Job Queue (RQ)

auto-gallery uses RQ (Redis Queue) as the job queue backend. Key design choices:

- **Why RQ**: Simpler than Celery, uses Redis already in the stack. `download_job`/`import_job` database tables are the source of truth; the queue backend is replaceable.
- **Job timeout**: `job_timeout=7200` (2 hours) on all enqueue calls to prevent RQ from killing long-running downloads (default is 180s).
- **State machine**: `pending → downloading → downloaded → importing → complete | failed | stale | paused`. Paused jobs skip execution. Stale detection marks stuck downloading jobs after 2x timeout. Partial import recovery on timeout/failure.

## Data Flow

### Download Flow
```
Scheduler (timezone-aware, configurable interval or daily schedule)
  → Check each subscription_source: is sync due?
    → create download_job (pending)
      → Worker picks up job (downloading)
        → Provider.build_gallerydl_config()
        → subprocess.run(["gallery-dl", "--write-metadata",
            "--range", "1-{max_posts}", ...], shell=False)
        → Files land in DOWNLOAD_ROOT/{source}/{creator_name}/{source_work_id}/
        → Job status = downloaded
        → Enqueue import_job
```

Key download details:
- `--write-metadata`: Required flag — without it, import runner has no JSON metadata to parse.
- `--range`: Limits download to the most recent N posts for incremental syncs. Configurable per subscription.
- **Scheduler**: Timezone-aware via the `TIMEZONE` env var (defaults to UTC). Supports interval mode (sync every N hours) or scheduled mode (sync at specific times daily).

### Import Flow
```
Worker picks up import_job
  → Scan DOWNLOAD_ROOT/{source}/ for *.json metadata files (--write-metadata output)
  → Provider.parse_work_source() → group JSONs by source_work_id
  → Provider.parse_source_creator() → upsert source_creator
  → Provider.parse_work_source() → upsert work_source
  → Create Asset + AssetSource from image files in work directory
  → Provider.parse_source_tags() → upsert tags + work_source_tags
  → Generate thumbnails (pyvips WebP) in LIBRARY_ROOT/{source}/{creator_name}/{source_work_id}/
  → Write metadata.json to LIBRARY_ROOT/{source}/{creator_name}/{source_work_id}/
  → Delete processed JSON files (image files remain in DOWNLOAD_ROOT)
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

## Authentication

Admin Web and backend API use JWT-based authentication:

- **Login**: POST `/api/v1/auth/login` with username/password returns an access token.
- **Token format**: JWT access tokens signed with the server's secret key.
- **Auth methods**: Admin endpoints require `Authorization: Bearer <jwt>`.
- **Token expiry**: Configurable via `ACCESS_TOKEN_EXPIRE_MINUTES` (default 30 minutes).
- **First-login rotation**: Bootstrap admin accounts are marked `must_change_password=true` and can only continue after changing password.
- All admin API routes require authentication via the `RequireAdmin` dependency.

## Backup System

The system includes automated backup capabilities:

- **Trigger**: POST `/api/v1/admin/backup` creates a full backup (pg_dump + config tarball + download archive listing).
- **Format**: Compressed tar.gz archive stored under `DOWNLOAD_ROOT/.backups/`.
- **Download**: GET `/api/v1/admin/backup/download` serves the backup file.
- **Listing**: GET `/api/v1/admin/backup` lists available backups.
- **Restore**: POST `/api/v1/admin/backup/restore` restores from a backup archive.
- **Scheduling**: POST `/api/v1/admin/backup/schedule` enables recurring auto-backup via RQ scheduler at configurable intervals. Old backups are pruned (last 5 kept).

## Integrity Check

An integrity verification system scans for data consistency issues:

- **Endpoint**: GET `/api/v1/admin/integrity-check`
- **Checks performed**:
  - Orphaned files in `downloads/` with no corresponding DB records
  - Missing thumbnails for assets that should have them
  - Orphaned DB records pointing to non-existent files
  - Work source / asset source consistency

## Container Paths

Application code uses only these env-var-driven paths:

| Variable | Container path | Purpose |
|---|---|---|
| `DOWNLOAD_ROOT` | `/downloads` | Original Media Store: long-term original files, organized by source/creator/work |
| `LIBRARY_ROOT` | `/library` | Library Index: per-work metadata + thumbnails |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl configs, cookies, and short-lived per-job configs |
| `APP_CONFIG_ROOT` | `/app-config` | Application runtime config |

NAS host paths are mapped in `docker-compose.yaml` only.

## NAS Storage Structure

```
/volume1/auto-gallery/
├── downloads/                          # DOWNLOAD_ROOT — Original Media Store
│   └── {source}/                       # e.g. pixiv, iwara, x
│       └── {creator_name}/             # e.g. ASK, 1980643
│           └── {source_work_id}/        # e.g. 38362603 (Pixiv artwork ID)
│               ├── 38362603_p0.jpg     # Page 0
│               └── 38362603_p1.jpg     # Page 1 (if multi-page)
│
├── library/                            # LIBRARY_ROOT — Library Index
│   └── {source}/                       # e.g. pixiv, iwara, x
│       └── {creator_name}/             # e.g. ASK, 1980643
│           └── {source_work_id}/        # Same ID as downloads — links the two
│               ├── metadata.json       # Work metadata export
│               └── thumbnail.webp      # 400px WebP thumbnail (pyvips)
│
├── config/
│   ├── gallery-dl/                     # GALLERYDL_CONFIG_ROOT
│   │   ├── config.json                 # gallery-dl base config
│   │   ├── cookies/                    # Auth cookies per source
│   │   └── jobs/                       # Short-lived worker job configs (auto-cleaned)
│   └── app/                            # APP_CONFIG_ROOT — runtime configs
│
├── docker/                             # Persistent volumes
│   ├── postgres/                       # PostgreSQL data
│   ├── redis/                          # Redis data
│   └── meilisearch/                    # Meilisearch data
│
└── backup/                             # Backups
    ├── db/                             # PostgreSQL dumps
    ├── config/                         # Config backups
    └── metadata/                       # metadata.json copies
```

### Storage rules

- **downloads/**: Original Media Store. Original image/video files are organized by `{source}/{creator_name}/{source_work_id}/` and kept long-term. Files are moved here from the flat gallery-dl output during import. No JSON metadata files kept — they are deleted after processing. No job_id layer in the path.
- **library/**: Library Index. Per-work metadata + thumbnail only. Same `{source}/{creator_name}/{source_work_id}/` structure as downloads, linked by source_work_id. No original images.
- **gallery-dl output**: Short-lived worker output before import. During import, files are reorganized into the long-term Original Media Store and JSON files are deleted.
- **Thumbnail**: 400px WebP, generated by pyvips from the first page image. Served from LIBRARY_ROOT via `/media/thumb/{asset_id}`.
- **Preview/Original**: Served directly from DOWNLOAD_ROOT via `/media/preview/{asset_id}` and `/media/original/{asset_id}`.
- **metadata.json**: Written per-work during import. Contains work_id, source, source_work_id, title, posted_at, creator, assets array.
- **source_work_id**: The platform-specific work ID (e.g. Pixiv artwork ID "38362603"). Used as the directory name in both downloads/ and library/. Links the two storage trees.

## Environment Variables

Key environment variables used by the application:

| Variable | Default | Purpose |
|---|---|---|
| `BACKEND_PORT` | `8818` | Host port for backend API (maps to container 8000) |
| `ADMIN_WEB_PORT` | `13000` | Host port for admin web (maps to container 3000) |
| `CORS_ORIGINS` | `http://localhost:13000` | Allowed CORS origins |
| `BACKEND_INTERNAL_URL` | `http://backend:8000` | Internal URL for admin-web SSR to reach backend |
| `DATABASE_URL` | (required) | PostgreSQL connection string |
| `REDIS_URL` | (required) | Redis connection string |
| `SECRET_KEY` | (required) | JWT signing secret |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | JWT token expiry |
| `TIMEZONE` | `UTC` | Timezone for scheduler and display |
| `DOWNLOAD_ROOT` | `/downloads` | Original file storage path |
| `LIBRARY_ROOT` | `/library` | Metadata + thumbnail storage path |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl configuration path |
| `APP_CONFIG_ROOT` | `/app-config` | Application config path |

## API Route Groups

All routes are prefixed with `/api/v1` unless otherwise noted.

### `/api/v1/system`
Health, queue stats, storage stats, clear failed jobs, log buffer.

Endpoints:
- `GET /health` — system health check (DB, Redis, Meilisearch, disk)
- `GET /health/disk` — disk usage stats
- `GET /queue-stats` — pending/running/failed job counts
- `GET /storage-breakdown` — per-source storage usage
- `POST /clear-failed-jobs` — clear all failed download/import jobs
- `GET /logs` — tail buffered log output

### `/api/v1/admin`
Settings, gallery-dl config, system info, integrity, backup, import progress, proxy testing, scheduler control, dedup, merge candidates, reindex, data cleanup.

Endpoints include:
- `GET /settings` / `PUT /settings` — system settings CRUD
- `POST /settings/reset` — reset settings to defaults
- `GET /gallerydl-config` / `PUT /gallerydl-config` — per-source gallery-dl extractor config
- `GET /system-info` — system overview (CPU, memory, disk, database size, archive sizes)
- `GET /integrity-check` — scan for orphaned files, missing thumbnails, orphaned records
- `POST /backup` — create full system backup (pg_dump + configs + archives)
- `GET /backup` — list available backups
- `GET /backup/download` — download a backup file
- `POST /backup/restore` — restore from a backup archive
- `POST /backup/schedule` — enable/disable recurring auto-backup
- `POST /cleanup-metadata-jsons` — remove stale JSON metadata files
- `GET /import-progress` — real-time import progress polling
- `POST /proxy/test` — test proxy connectivity with gallery-dl
- `POST /scheduler/sync-now` — trigger immediate subscription sync
- `POST /clear/{entity}` — clear all records of a given entity type
- `GET /dedup` / `PUT /dedup` — deduplication settings
- `GET /merge-candidates` — list works flagged for potential merge
- `POST /reindex` — trigger full Meilisearch reindex

### `/api/v1/creators`
Creator CRUD, count, timeline, duplicate detection, merge, favorite, source-account binding, creator links.

Endpoints include:
- `GET /` — list creators (paginated, searchable)
- `POST /` — create creator
- `GET /count` — total creator count
- `GET /{id}` — creator detail with source accounts and links
- `PUT /{id}` — update creator
- `DELETE /{id}` — delete creator
- `GET /{id}/timeline` — publishing activity timeline
- `GET /duplicates` — detect potential duplicate creators
- `POST /merge` — merge two creators into one
- `POST /{id}/favorite` / `DELETE /{id}/favorite` — toggle favorite
- `POST /{id}/source-accounts` — bind a source_creator to this creator
- `DELETE /{id}/source-accounts/{sc_id}` — unbind a source_creator
- `GET /{id}/links` / `POST /{id}/links` — manage creator_url links
- `PUT /links/{link_id}` — update link (approve/reject)

### `/api/v1/subscriptions`
Subscription CRUD, source toggling, batch operations, sync triggers.

Endpoints include:
- `GET /` — list subscriptions
- `POST /` — create subscription
- `GET /count` — total subscription count
- `GET /{id}` — subscription detail with sources
- `PUT /{id}` — update subscription
- `DELETE /{id}` — delete subscription
- `POST /batch-delete` — delete multiple subscriptions
- `POST /batch-toggle-sync` — toggle sync for multiple subscriptions
- `POST /{id}/sync-now` — trigger immediate sync for this subscription
- `GET /{id}/sources` — list sources for a subscription
- `PUT /{id}/sources/{source}` — enable/disable/toggle a subscription source

### `/api/v1/download-jobs`
Download job queue management with batch operations and per-job control.

Endpoints include:
- `GET /` — list download jobs (paginated, filterable by status)
- `POST /` — create download job
- `GET /{id}` — job detail
- `DELETE /{id}` — delete job
- `POST /clear` — clear all completed/failed jobs
- `POST /kill-stuck` — kill all stuck jobs (stale detection)
- `POST /retry-all` — retry all failed jobs
- `POST /batch-delete` — batch delete jobs
- `POST /{id}/pause` — pause a job
- `POST /{id}/resume` — resume a paused job
- `POST /{id}/retry` — retry a specific failed job

### `/api/v1/import-jobs`
Import job queue and post-import scanning.

Endpoints include:
- `GET /` — list import jobs
- `GET /{id}` — import job detail with errors
- `POST /{id}/retry` — retry a failed import
- `POST /scan` — scan for unimported downloads and create import jobs

### `/api/v1/works`
Work browser with batch operations, tagging, and source detail.

Endpoints include:
- `GET /` — list works (paginated, filterable)
- `GET /{id}` — work detail
- `PUT /{id}` — update work
- `DELETE /{id}` — delete work
- `POST /batch-delete` — batch delete works
- `POST /batch-tag` — batch add/remove tags
- `GET /{id}/assets` — list assets for a work
- `GET /{id}/sources` — list work_sources for a work
- `GET /{id}/tags` — list tags for a work
- `POST /{id}/favorite` / `DELETE /{id}/favorite` — toggle favorite

### `/api/v1/tags`
Tag management with alias and merge support.

Endpoints include:
- `GET /` — list tags
- `POST /` — create tag
- `GET /{id}` — tag detail
- `PUT /{id}` — update tag
- `DELETE /{id}` — delete tag
- `POST /{id}/alias` — set tag alias
- `POST /merge` — merge two tags into one

### `/api/v1/search`
Meilisearch-powered full-text search and index management.

Endpoints include:
- `GET /` — search works, creators, tags
- `POST /reindex` — trigger full Meilisearch reindex

### `/api/v1/sources`
Source provider listing and capabilities.

Endpoints:
- `GET /` — list all registered source providers with capabilities
- `GET /{source}` — specific provider capabilities

### `/api/v1/reference`
Danbooru reference provider operations for creator identity mapping.

Endpoints include:
- `POST /danbooru/preview` — preview Danbooru artist tag results
- `POST /danbooru/import` — import a single Danbooru artist reference
- `POST /danbooru/batch-import` — batch import from Danbooru

### `/api/v1/auth`
Authentication endpoints.

Endpoints:
- `POST /login` — login, returns JWT access token
- `GET /me` — current authenticated user info
- `POST /change-password` — change password for current user

### `/api/v1/admin/naming-templates`
Naming template management for download directory structure.

Endpoints include:
- `GET /` — list naming templates
- `POST /` — create naming template
- `GET /{id}` — template detail
- `PUT /{id}` — update template
- `DELETE /{id}` — delete template
- `POST /preview` — preview directory structure from a template
- `POST /{id}/set-default` — set as default template

### `/media` (unversioned)
Controlled media file serving. Not prefixed with `/api/v1`.

Endpoints:
- `GET /thumb/{asset_id}` — 400px WebP thumbnail from LIBRARY_ROOT
- `GET /preview/{asset_id}` — original file from DOWNLOAD_ROOT (for preview)
- `GET /original/{asset_id}` — original file from DOWNLOAD_ROOT (for download)
