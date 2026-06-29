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
│  Providers: Pixiv · X · Iwara · Danbooru ·      │
│   Weibo · Bilibili · Pinterest · LOFTER          │
│   (+ Danbooru reference; local/manual planned)   │
│  Port 8818 (host) ← 8000 (container)             │
│  JWT auth (access + refresh tokens)              │
└──────┬────────────┬──────────────┬──────────────┘
       │            │              │
       │   ┌────────▼────────────┐ │
       │   │  Workers (RQ)       │ │
       │   │  worker-download    │ │  gallery-dl subprocess
       │   │  worker-import      │ │  metadata → DB pipeline
       │   │  worker-operations  │ │  backup / reindex / disk-import
       │   │  scheduler          │ │  subscription sync loop
       │   └────────┬────────────┘ │
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

(user-scoped)
user ──< subscription ──< subscription_source
user ──< album ──< album_work ── work
user ──< download_job
import_job (inherits scope via subscription)

(operational / cross-cutting)
task_run ──< task_event        # unified task envelope — see "Unified Task System"
storage_artifact               # download → import ledger — see "Job Queue"
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

## Job Queue (RQ + Redis Stream)

auto-gallery uses RQ (Redis Queue) for downloads and batch imports, plus Redis Stream for event-driven imports. Key design choices:

- **Why RQ**: Simpler than Celery, uses Redis already in the stack. `download_job`/`import_job` database tables are the source of truth; the queue backend is replaceable.
- **Per-source download queues**: Each source has its own RQ queue (`downloads:pixiv`, `downloads:danbooru`, etc.) for isolation — one slow source never blocks another. The `worker-download` container listens on all source queues.
- **Durable RQ imports**: Download artifacts are recorded in PostgreSQL and a single RQ import pipeline claims work with leases, enabling restart-safe recovery without competing consumers.
- **Job timeout**: `job_timeout=7200` (2 hours) on all enqueue calls to prevent RQ from killing long-running downloads (default is 180s).
- **State machine** (canonical source: `backend/app/models/task_state.py`): `enqueued → downloading → downloaded → importing → complete`, with `paused`, `cancelled`, `failed`, and `stale` reachable from the non-terminal states. Old status strings (`pending` → `enqueued`) are accepted via a backward-compat map. A `downloaded → complete` short-circuit exists for jobs whose download produced no new metadata (all content already in the archive), so they never get stuck waiting on an import. Paused jobs skip execution; `cancelled` is terminal but keeps the DB record for audit. Stale detection uses Redis heartbeat keys (`task:{job_id}:heartbeat_ts` with 90s TTL) — if the key is absent and the job is past its retry grace period, it's marked stale. Partial import recovery on timeout/failure. `TaskEngine` (`services/task_engine.py`) is the single authority that validates every transition.
- **Adaptive timeouts**: Retry count increments the stall and deadline timeouts (1.0x → 1.5x → 2.0x) to give later attempts more headroom.
- **Proxy pre-flight**: Before launching gallery-dl, the worker runs DNS resolution + proxy connectivity checks (non-blocking diagnostics).

## Unified Task System (`task_runs`)

Downloads, imports, and admin operations (backup, reindex, disk import) are all surfaced through one user-facing task envelope so the admin **Tasks** page (`/admin/jobs`) has a single queue/progress/history view.

- **`task_run`** (`models/task_run.py`): one row per trackable unit of work. `kind` (`download` / `import` / `admin`) and `subject_type` + `subject_id` loosely reference the authoritative domain row (`download_job`, `import_job`, …) — there is no FK, so a task can outlive or precede its domain record. Carries display fields (`title`, `source`, `source_url`), a progress snapshot (`progress_current`/`progress_total`/`progress_data`), `priority`, `attempts`, and `last_heartbeat_at` for stale detection. `parent_task_id` groups batch/child runs.
- **`task_event`**: append-only audit trail of status transitions for a run; cascade-deleted with its parent (`ON DELETE CASCADE`).
- **`TaskService`** (`services/tasks.py`): the write path — creates runs, records events, normalizes statuses, and merges download/import domain rows into the unified list.
- **`TaskEngine`** (`services/task_engine.py`): the single authority for valid state transitions (pause / resume / cancel / retry / priority / batch-by-filter), publishing changes over Redis pub/sub for live WebSocket updates.
- **Authoritative payloads stay in domain tables**: `task_run` is the envelope; `download_jobs` / `import_jobs` remain the source of truth for their own fields. Clearing tasks (Data Management) deletes `task_runs` **and** the domain job tables together.

## Data Flow

### Download Flow
```
Scheduler (timezone-aware, configurable interval or daily schedule)
  → Check each subscription_source: is sync due?
    → create download_job (pending)
      → Enqueue to per-source queue (downloads:{source})
      → Worker picks up job (downloading)
        → Proxy/DNS pre-flight checks
        → Provider.build_gallerydl_config()
        → subprocess.Popen(["gallery-dl", ...], shell=False)
            with stall detection polling loop
        → Files land in DOWNLOAD_ROOT/{source}/{creator_name}/{source_work_id}/
        → FileIndex registration (SQLite)
        → Job status = downloaded
        → PostgreSQL storage_artifacts → RQ import job
```

Key download details:
- `--write-metadata`: Required flag — without it, import runner has no JSON metadata to parse.
- `--range`: Limits download to the most recent N posts for incremental syncs. Configurable per subscription.
- **Scheduler**: Timezone-aware via the `TIMEZONE` env var (defaults to UTC). Supports interval mode (sync every N hours) or scheduled mode (sync at specific times daily).
- **Stall detection**: Polling loop checks progress every 4s; kills gallery-dl if no file output for `stall_timeout` seconds (default 120s, after 30s grace period). Adaptive timeout on retries.
- **Three-layer timeout**: (1) gallery-dl internal `--retries`/`--abort`, (2) stall detection kills frozen subprocess, (3) overall deadline timeout.

### Import Flow

**Event-driven (primary):**
```
Download job completes → storage_artifacts batch UPSERT
  → worker-import claims work with a lease
  → FileIndex.query() finds new files by work_id (O(log n), not rglob)
  → Provider.parse_work_source() → group files by source_work_id
  → Provider.parse_source_creator() → upsert source_creator
  → Provider.parse_work_source() → upsert work_source
  → Create Asset + AssetSource from image files in work directory
  → Provider.parse_source_tags() → upsert tags + work_source_tags
  → Generate thumbnails (pyvips WebP) in LIBRARY_ROOT/{source}/{creator_name}/{source_work_id}/
  → Write metadata.json to LIBRARY_ROOT/{source}/{creator_name}/{source_work_id}/
  → FileIndex.mark_imported()
  → mark artifact work done/failed
```

**Batch import (RQ, for Danbooru reference):**
```
Worker picks up import_job from imports queue
  → FileIndex.query_unimported() → new files since last scan
  → Same parse → upsert → thumbnail → metadata.json pipeline
  → Job status = complete
```

Key details:
- JSON metadata files are deleted after processing; image files remain in DOWNLOAD_ROOT.
- FileIndex (SQLite) replaces rglob scans — O(log n) lookups by work_id instead of O(n) filesystem traversal.
- Consumer group `import-workers` ensures at-least-once delivery: if a consumer crashes mid-import, the pending message is reclaimed by another consumer.

### Danbooru Reference Flow
```
Admin triggers Danbooru artist import
  → Fetch Danbooru artist page
  → Parse artist tags, related URLs
  → Create creator_link suggestions (status=pending)
  → Admin reviews: approve → bind to creator, reject → discard
```

## Reliability & Recovery

Because gallery-dl writes directly to disk and the download → import handoff spans two queues, several recovery paths keep the database in sync with what is actually on disk. All are idempotent and never delete or re-download.

- **Import from disk** (`services/disk_import.py`, `POST /api/v1/admin/library/import-from-disk`): scans `DOWNLOAD_ROOT/{source}/` and imports files that exist on disk but were never recorded (ledger state ≠ `done`), under a synthetic `recovery` download_job. `services/disk_identity.py` first provisions a real `creator → subscription → subscription_source → source_creator` chain from local metadata (Danbooru enrichment is best-effort, with a metadata-only fallback) so recovered works attach to the right creator. Runs on the `operations` queue; a Redis lock (`library:disk-import:active`) prevents concurrent runs.
- **Import pipeline recovery** (`services/import_recovery.py`): conservative auto-repair of safe gaps — e.g. re-enqueues an import for `downloaded` jobs that already have pending metadata. Read-only health is reported by `services/pipeline_health.py`.
- **Creator auto-relink** (`services/creator_reconcile.py`): each subscription-sync scan links orphaned `source_creators` (`creator_id IS NULL`) to their subscription's creator by identity match. It mirrors `import_runner`'s auto-link rules, so an ambiguous bookmark/repost feed is never mislinked.
- **Worker startup orphan reconciliation**: on startup, jobs stuck in `downloading`/`importing` are marked `stale`, and on-disk directories with no matching job are picked up for import.

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
- **Thumbnail**: 400px WebP, generated by pyvips from the first page image. Served from LIBRARY_ROOT via unauthenticated `/media/thumb/{asset_id}` for grid/list image tags.
- **Preview/Original**: Served directly from DOWNLOAD_ROOT via `/media/preview/{asset_id}` and `/media/original/{asset_id}`. Preview requires a short-lived signed URL; original accepts either admin Bearer auth or a short-lived signed URL.
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
| `NEXT_PUBLIC_WS_URL` | current site `/api/v1/ws` | Public browser WebSocket URL for live job updates |
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
- `POST /library/import-from-disk` — idempotently import on-disk download files into the DB without re-downloading (operations queue)
- `POST /clear/{entity}` — clear all records of a given entity type (`jobs`/`all` also clear `task_runs`)
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
- `POST /{id}/cancel` — cancel a job (terminal, keeps record) — TaskEngine
- `POST /{id}/priority` — change queue priority — TaskEngine
- `POST /batch-by-filter` — apply an action to all jobs matching a filter — TaskEngine
- `GET /{id}/progress` — live progress snapshot
- `GET /{id}/pipeline` — per-stage pipeline view

### `/api/v1/import-jobs`
Import job queue and post-import scanning. List entries are enriched with parent download-job context (source, url, subscription/creator) so they align with the download list.

Endpoints include:
- `GET /` — list import jobs (enriched with source/subscription/creator)
- `GET /{id}` — import job detail with errors
- `POST /{id}/retry` — retry a failed import
- `POST /{id}/cancel` — cancel an import — TaskEngine
- `POST /{id}/priority` — change queue priority — TaskEngine
- `POST /batch-by-filter` — apply an action to all imports matching a filter — TaskEngine
- `GET /{id}/progress` — live progress snapshot
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


### `/media` (unversioned)
Controlled media file serving. Not prefixed with `/api/v1`.

Endpoints:
- `GET /thumb/{asset_id}` — 400px WebP thumbnail from LIBRARY_ROOT
- `GET /preview/{asset_id}` — original file from DOWNLOAD_ROOT, requires a short-lived signed URL
- `GET /original/{asset_id}` — original file from DOWNLOAD_ROOT (for download)
