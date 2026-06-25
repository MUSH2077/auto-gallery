# auto-gallery

NAS-hosted multi-source media archive and gallery manager. Downloads, imports, indexes, and manages creator-based media from 8 platforms through Docker Compose.

## Communication (语言规范)

- 默认用中文（简体）回复用户。Reply to the user in Chinese (Simplified) by default.
- 代码、标识符、commit message、文件内容保持原语言（通常为英文），不要翻译。

## Quick Start

```bash
docker compose build
docker compose exec backend alembic upgrade head
docker compose up -d --force-recreate backend worker-download worker-import scheduler admin-web
docker compose exec backend python -m pytest
```
Backend API: `8818`, Admin Web: `13000`.

## Stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2
- **Infra**: PostgreSQL 16, Redis 7, RQ (job queue), Meilisearch (search)
- **Media**: gallery-dl (download), pyvips (thumbnails), ffmpeg (video)
- **Frontend**: Next.js 14, React 18, TypeScript 5, Tailwind CSS, TanStack Query
- **Deploy**: Docker Compose v2, NAS LAN first, no public internet

## Services

| Service | Role |
|---------|------|
| `backend` | FastAPI API server |
| `worker-download` | RQ worker — gallery-dl subprocess (queue: `downloads`) |
| `worker-import` | RQ worker — metadata import, batch import (queue: `imports`) |
| `scheduler` | RQ scheduler — subscription sync loop |
| `admin-web` | Next.js admin interface |
| `postgres`, `redis`, `meilisearch` | Data services |

Backend, workers, and scheduler share the same Docker image. Only workers execute gallery-dl.

## Providers

11 providers, 8 downloadable:

| Provider | Download | Key config |
|----------|----------|------------|
| Pixiv | gallery-dl | Cookie auth |
| X/Twitter | gallery-dl | Cookie auth, `strategy: "tweets"` |
| Iwara | gallery-dl | Username/password or cookies |
| Danbooru | gallery-dl | API key or cookies |
| Weibo | gallery-dl | Optional cookies |
| Bilibili | gallery-dl | Public |
| Pinterest | gallery-dl | Public |
| LOFTER | gallery-dl | Public |
| Danbooru Reference | Reference only | Artist identity mapping |
| Local Folder | Planned | — |
| Manual Upload | Planned | — |

Provider interface: `source_name`, `display_name`, `capabilities`, `normalize_url()`, `validate_url()`, `build_gallerydl_config()`, `parse_source_creator()`, `parse_work_source()`, `parse_assets()`, `parse_source_tags()`. Source-specific logic lives only in `providers/`.

## Domain Model

Source-agnostic naming (never Pixiv-specific):

`creator` → `source_creator` (per-platform identity) → `creator_link` (cross-platform URLs)
`subscription` → `subscription_source` (per-source sync config)
`work` → `work_source` (per-platform record) → `asset` → `asset_source`
`tag` → `work_tag` + `work_source_tag` (preserve original tags)
`download_job` → `import_job` (two-phase lifecycle)

Uniqueness: `source_creators(source, source_creator_id)`, `work_sources(source, source_work_id)`, `subscription_sources(subscription_id, source)`, `tags(normalized_name)`, `users(username)`.

## Key Constraints

- `shell=True` FORBIDDEN — gallery-dl via `subprocess.Popen([...], shell=False)`
- URL validation at API boundary before job creation
- All admin APIs require JWT auth (`RequireAdmin`)
- `/media/thumb/` open (img tags), `/media/preview/` + `/media/original/` require auth
- Path containment via `Path.relative_to()`, never string prefix matching
- Never expose NAS filesystem paths to clients
- `PGPASSWORD` in subprocess env — callers must not log the parsed DB dict
- `SECRET_KEY` + `ADMIN_PASSWORD` validated at config load, refuse startup if `changeme`
- Default passwords in `.env` only, `.env` is git-ignored

## Architecture

```
API route (thin) → Service (business logic) → Repository (data access) → Model
```

- **Routes**: parameter parsing only, call services, map exceptions to HTTP
- **Services** (`services/`): `CreatorService`, `SubscriptionService`, `DownloadService`, `SearchService`, plus cache, locks, job state/manifest, settings, proxy, thumbnail, Danbooru client, dedup — see `services/__init__.py`
- **Repositories** (`repositories/`): `CreatorRepository`, `WorkRepository`, `SubscriptionRepository`, `DownloadJobRepository`, `TagRepository` — pure data access, no external calls
- **Jobs** (`jobs/`): `run_download_job()`, `run_import_job()`, `sync_subscriptions()`, `run_batch_import()` — RQ entry points
- Shared Redis via `get_redis()` in `services/redis_client.py`
- Async distributed lock via `redis_lock()` in `services/locks.py`

## Job Pipeline

```
subscription_source enabled → enqueue download_job (downloads queue)
  → gallery-dl subprocess → files + metadata JSONs
  → enqueue import_job (imports queue)
  → parse metadata → create Work/WorkSource/Asset/AssetSource/Tags
  → thumbnails (pyvips), pHash, sha256 → Meilisearch index
  → mark complete
```

- RQ timeout: 7200s on all enqueue calls
- gallery-dl exit code 0 does NOT guarantee files — check metadata JSON count
- `--write-metadata` required on gallery-dl command
- Batch import (Danbooru) goes to `imports` queue, dedup by source type not URL
- Import worker also processes batch import — same container, same queue

## Storage

- `DOWNLOAD_ROOT` (/downloads): original files, `{source}/{naming_template_output}/{source_work_id}/`
- `LIBRARY_ROOT` (/library): `{source}/{creator_dir}/{source_work_id}/metadata.json` + `thumbnail.webp`
- `GALLERYDL_CONFIG_ROOT`: `config.json`, `cookies/`, `jobs/` (temp, auto-cleaned)
- `APP_CONFIG_ROOT`: runtime configs, `secret_key`, `bootstrap_admin_password`
- gallery-dl writes directly to final location — files NOT moved after download
- Metadata JSONs deleted after import — use before/after snapshots to detect new files

## Gotchas

- Pixiv AI detection: `illust_ai_type=2` ONLY (1 = human)
- LOFTER directory: MUST use `["lofter", "{blog_name}", "{id}"]`
- `auto_enable_on_import` in `config.json` per-extractor, NOT in DB `system_settings`
- Only Pixiv defaults to `is_enabled=True` on import; others default disabled
- `_classify_url()` in danbooru.py: tighten regexes (e.g. `www.lofter.com` must not match LOFTER)
- `subscription_sources` unique on `(subscription_id, source)` — one per source type per subscription
- Worker containers need `ADMIN_PASSWORD` env var (config.py validates at import time)
- Alembic versions are baked into image via `COPY . .` — no bind mount needed

## Skills First (MANDATORY)

Before ANY task — whether a question, bug investigation, feature request, or code change — check whether a relevant skill exists and invoke it. Never skip this step, even for seemingly trivial tasks.

- Check the available skills list in the session context for matches
- If even a 1% chance a skill applies, invoke it via the Skill tool FIRST
- Skills determine HOW to approach the task (debugging, planning, code review, etc.)
- Never rationalize skipping: "this is simple" "I know this already" "let me explore first" are all wrong

## Constraint Docs (load on demand)

Detailed non-negotiable rules live in `.claude/constraints/`. Read the relevant file BEFORE touching that area (the `/plan` command loads all of them; otherwise pull the ones below as needed):

- `security.md` — subprocess `shell=False`, URL validation at boundary, auth, CORS, `/media` serving
- `source-abstraction.md` — source-agnostic naming, the provider interface
- `filesystem-paths.md` — `Path.relative_to` containment, `DOWNLOAD_ROOT`/`LIBRARY_ROOT`
- `metadata-detection.md` — before/after snapshot, gallery-dl metadata JSON detection
- `deduplication.md` — dedup defaults OFF, no auto-merge/auto-delete
- `creator-display-name.md` · `danbooru-reference.md` — creator identity & Danbooru reference-only rules
- `docker.md` · `deployment.md` · `remote-access.md` — Compose, NAS deploy flow, remote access
- `frontend-discipline.md` · `client-api.md` · `source-colors.md` — frontend, typed API client, per-source colors
- `tech-stack.md` · `verification.md` · `commit-checklist.md` — stack, verification, commit gate

## Coding Rules

- Business logic in services, never in routes
- Pydantic schemas for all API responses
- Import operations in transactions; don't let one failed asset break a full import
- Cache TTLs centralized in `services/cache.py` `TTL` registry
- `__all__` exports in each package's `__init__.py` document public API
- Provider registration via `init_providers()` factory in `providers/__init__.py`
- Batch operations return per-item results, never silently swallow errors
- Frontend: typed API client, TanStack Query, thin pages, i18n keys natural-language
- Commit immediately after each completed task

## Completion Workflow (MANDATORY)

After completing any non-trivial code change, execute these steps in order:

### 1. Verify — run tests

```bash
docker compose exec backend python -m pytest -v
```

All tests must pass. Do NOT proceed past a failure.

### 2. Deploy — rebuild and restart

Backend code is baked into the Docker image via `COPY . .`; stale containers run old code.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" --pull backend
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler
```

Or: `bash scripts/deploy.sh`

- Also applies to frontend changes (`admin-web/**`) — add `admin-web` to build + restart list.
- `CACHEBUST` is required; without it Docker may reuse a stale `COPY . .` layer.
- Verify: `docker compose logs worker-import --tail=20`

### 3. Commit — record the change

```bash
git add -A
git commit -m "<conventional-commit-message>"
```

Always end commit messages with:
```
Co-Authored-By: Claude <noreply@anthropic.com>
```
