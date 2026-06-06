# auto-gallery Project Guide

## Quick Start

```bash
# Build all services
docker compose build

# Build specific services
docker compose build backend admin-web

# Run database migrations
docker compose exec backend alembic upgrade head

# Deploy all services (backend code changes affect all three)
docker compose up -d --force-recreate backend worker scheduler admin-web

# View logs
docker compose logs --tail 50 -f backend

# Run tests
docker compose exec backend python -m pytest

# Backend API runs on port 8818, Admin Web on port 13000
```

## Project Identity

Project name: auto-gallery

auto-gallery is a NAS-hosted multi-source media archive and gallery management system. It downloads, imports, indexes, and manages creator-based media libraries from multiple sources.

The project must not be hard-coded to Pixiv.

Pixiv is only one source provider.

Danbooru is not treated as the complete union of all works. In the current phase, Danbooru is used primarily as a reference provider for creator identity mapping through artist tags and related URLs.

Future supported sources may include:
- Pixiv
- Iwara
- X/Twitter
- Danbooru posts
- local folder import
- manual upload
- other gallery-dl compatible sites

## Current Priority

Build the server and admin management system first.

Do not build the Flutter client yet.

All backend APIs must be stable and suitable for future Flutter/mobile clients.

## Documentation

Detailed docs in `/docs/`. Every `.md` file has a `.zh.md` Chinese counterpart that must be kept in sync.

English / 中文:
- [architecture.md](../docs/architecture.md) / [architecture.zh.md](../docs/architecture.zh.md) — architecture overview, domain model, data flow
- [setup.md](../docs/setup.md) / [setup.zh.md](../docs/setup.zh.md) — installation, configuration, gallery-dl auth
- [development.md](../docs/development.md) / [development.zh.md](../docs/development.zh.md) — local dev, tests, debugging
- [providers.md](../docs/providers.md) / [providers.zh.md](../docs/providers.zh.md) — provider interface, registry, per-provider status
- [risks.md](../docs/risks.md) / [risks.zh.md](../docs/risks.zh.md) — risk register with mitigations and decisions

Constraint and skill files in `.claude/`:

Skills (in `.claude/skills/`):
- [gallerydl-integration.md](../.claude/skills/gallerydl-integration.md) — gallery-dl execution, config, state machines, auth tracking
- [backend-architecture.md](../.claude/skills/backend-architecture.md) — layered architecture, models, API conventions
- [provider-design.md](../.claude/skills/provider-design.md) — provider interface, registry, adding new providers
- [nas-deployment.md](../.claude/skills/nas-deployment.md) — NAS host setup, .env, volumes

Constraints (in `.claude/constraints/`):
- [tech-stack.md](../.claude/constraints/tech-stack.md) — full technology stack with versions and exclusions
- [source-abstraction.md](../.claude/constraints/source-abstraction.md) — generic model naming, provider interface
- [danbooru-reference.md](../.claude/constraints/danbooru-reference.md) — Danbooru as reference only
- [deduplication.md](../.claude/constraints/deduplication.md) — dedup policy, opt-in only
- [docker.md](../.claude/constraints/docker.md) — services, container paths, volume mapping
- [security.md](../.claude/constraints/security.md) — subprocess, URL validation, auth (admin + JWT), CORS, /media
- [filesystem-paths.md](../.claude/constraints/filesystem-paths.md) — naming templates, directory structure, path assumptions
- [creator-display-name.md](../.claude/constraints/creator-display-name.md) — canonical display_name, name resolution hierarchy, UI consistency
- [source-colors.md](../.claude/constraints/source-colors.md) — canonical source colors, single palette, anti-duplication
- [metadata-detection.md](../.claude/constraints/metadata-detection.md) — always verify field values against gallery-dl source before implementing detection
- [client-api.md](../.claude/constraints/client-api.md) — user model, client-facing APIs, albums, mobile response design
- [remote-access.md](../.claude/constraints/remote-access.md) — LAN/VPN/reverse-proxy, HTTPS, network architecture

Read constraints before implementing. The risk register documents decisions made during architecture review.

## Risk-Derived Decisions

The following were decided during risk analysis (see [docs/risks.md](../docs/risks.md) for rationale):

- **RQ for job queue** (not Celery). Simpler, uses Redis already in stack. `download_job`/`import_job` tables are source of truth; queue backend is replaceable.
- **`creator_link.confidence` field** (0.0–1.0 float). Suggested links from Danbooru or URL extraction are scored. Admin reviews low-confidence suggestions.
- **Job state machine**: Two separate lifecycles — `download_job` and `import_job`. Download: `pending → downloading → downloaded → complete | failed | stale | paused`. Import: `pending → running → complete | failed`. Paused jobs skip execution. Stale detection marks stuck downloading jobs after 2x timeout. Partial import recovery on timeout/failure. See [gallerydl-integration.md](../.claude/skills/gallerydl-integration.md) for the full state machine.
- **`subscription_source.last_successful_auth`** timestamp for cookie expiration visibility.
- **Meilisearch sync**: admin-triggered full re-indexing for v1. Application-level dual-writes deferred.
- **pyvips preferred over Pillow** for image processing (faster, lower memory).
- **X/Twitter provider is explicitly "no timeline"**. Placeholder with `can_download = False`. Re-evaluate after Pixiv pipeline is stable.

## Core Stack

Backend:
- Python
- FastAPI
- SQLAlchemy 2.0
- Alembic
- PostgreSQL
- Redis
- RQ
- Meilisearch
- gallery-dl
- Pillow or pyvips

Admin Web:
- Next.js
- React
- TypeScript
- Tailwind CSS
- TanStack Query

Deployment:
- Docker Compose
- NAS local network first
- No direct public internet exposure in the first version

## Docker Runtime

auto-gallery is a Docker Compose based project.

Required services:
- postgres
- redis
- meilisearch
- backend
- worker
- scheduler
- admin-web

gallery-dl must be included in the Dockerized runtime.

Do not assume gallery-dl is installed on the NAS host.

The backend image may include gallery-dl, but only the worker service should execute gallery-dl.

backend API processes must not execute gallery-dl directly.

backend, worker, and scheduler can share the same backend image with different commands.

gallery-dl configuration, cookies, and temporary job configs must be stored under GALLERYDL_CONFIG_ROOT.

A separate gallery-dl downloader container can be introduced later, but is not required in the first version.

## Core Concepts

Use generic domain concepts:

- source
- user (added Phase 6+, required before any remote client)
- creator
- source_creator
- creator_link
- work
- work_source
- asset
- asset_source
- tag
- work_tag
- work_source_tag
- subscription
- subscription_source
- download_job
- import_job
- naming_template
- album (added Phase 6+, user-scoped work collections)
- album_work
- reference_provider
- source_provider

Do not use Pixiv-specific domain model names such as pixiv_user, pixiv_illust, or pixiv_image.

Pixiv-specific logic must live only inside provider modules.

## Danbooru Role

Danbooru serves two roles:
1. **Reference provider** (`danbooru_reference`) — artist identity mapping, URL extraction, creator link suggestions
2. **Download provider** (`danbooru`) — post download via tag search (`posts?tags=artist_name`)

Danbooru has rich tag metadata (artist/character/copyright/general/meta categories).

Danbooru must not be assumed to contain the complete union of all works.
Danbooru subscription sources default to `is_enabled=False` on import — must be manually enabled.

## Creator Identity Mapping

The system must support the same creator having multiple accounts across multiple platforms.

Use:

- creators: canonical local creator identity
- source_creators: source-specific creator accounts
- creator_links: URLs and mappings related to the creator

A creator may have:
- Pixiv profile URL
- X/Twitter profile URL
- Iwara profile URL
- Danbooru artist tag reference
- personal website
- other links

The admin web must provide creator management:
- list creators
- view creator detail
- view source accounts
- add source account manually
- bind source account to creator
- merge creators
- split or unlink source account
- import Danbooru artist reference URLs
- approve or reject suggested mappings

## Subscription Source Selection

A subscription represents a creator the user wants to follow.

Each subscription may have multiple selected sync sources.

Example:

Subscription: Creator A

Selected sources:
- Pixiv: enabled
- X/Twitter: enabled
- Iwara: disabled
- Danbooru posts: disabled
- Local folder: disabled

Use a separate subscription_sources table.

A user must be able to choose sources through the admin web.

Do not assume one subscription equals one source.

## Download Behavior

When a user subscribes to a creator, the user can choose which sources to sync.

For each enabled subscription source:
- validate the source URL
- create a download job
- execute gallery-dl when supported
- write logs
- import downloaded metadata and media

All registered providers (Pixiv, Danbooru, Iwara, X, Pinterest, LOFTER) support gallery-dl download.
Import defaults: only Pixiv auto-enables subscription sources. Other sources default to disabled.
Per-source `auto_enable_on_import` configurable in gallery-dl settings page.

## Deduplication Policy

Strong deduplication must be configurable.

Default:
- strong deduplication is OFF
- do not skip downloads based on cross-source similarity
- do not delete files automatically
- do not automatically merge visually similar works
- store hashes and create duplicate or merge candidates where appropriate

When strong deduplication is enabled:
- source + source_work_id already exists may be skipped
- sha256-identical assets may reuse existing asset records
- exact duplicates may be linked via asset_sources instead of saving duplicate files

Visual similarity deduplication must not auto-merge by default.

pHash / perceptual similarity should create merge candidates for admin review.

## Work and Source Model

Do not treat a source work as the canonical local work.

Use:

- works: canonical local works
- work_sources: source-specific work records
- assets: local files
- asset_sources: source-specific file records

A local work may have multiple work_sources.

Example:
Work #100
- Pixiv source work
- X source work
- Iwara source work

Each work_source preserves:
- source
- source_work_id
- source_url
- source_creator_id
- title
- description
- posted_at
- raw_metadata

## Source Tags

The system must preserve source-specific tags.

Use:
- tags: normalized local tag table
- work_tags: local aggregated tags
- work_source_tags: original source tags for each work_source

Do not discard source tags.

Examples:
- Pixiv original tags
- X hashtags
- Iwara categories
- Danbooru normalized tags

The admin web should show which source each tag came from.

## Source Provider Architecture

Source-specific behavior must go through provider modules.

Provider modules:
- backend/app/providers/base.py
- backend/app/providers/registry.py
- backend/app/providers/pixiv.py
- backend/app/providers/iwara.py
- backend/app/providers/x.py
- backend/app/providers/danbooru.py (download provider — tag search)
- backend/app/providers/danbooru_reference.py (reference provider — artist identity)
- backend/app/providers/pinterest.py
- backend/app/providers/lofter.py
- backend/app/providers/local.py
- backend/app/providers/manual.py

Provider responsibilities:
- source_name
- display_name
- capabilities
- normalize_url(input_text)
- validate_url(url)
- build_gallerydl_config(subscription_source, naming_template)
- parse_source_creator(raw_metadata)
- parse_work_source(raw_metadata)
- parse_assets(raw_metadata, files)
- parse_source_tags(raw_metadata)

Danbooru reference provider responsibilities:
- normalize Danbooru artist tag input
- fetch/store artist reference records if implemented
- parse related URLs
- create suggested creator_links
- do not assume Danbooru is a complete source of works

## NAS Storage Structure

The directory layout under `downloads/` and `library/` is determined by per-source naming templates configured at `/admin/settings/gallerydl`. The diagram below shows the **default** structure — the actual layout depends on user configuration. See [filesystem-paths.md](../.claude/constraints/filesystem-paths.md) for the constraint.

### Default layout (using default naming templates)

```
/volume1/auto-gallery/
├── downloads/                          # DOWNLOAD_ROOT — original image files
│   └── {source}/                       # e.g. pixiv, iwara, x (top-level = provider.source_name)
│       └── {template_output}/          # Determined by naming template directory pattern
│           └── {source_work_id}/        # e.g. 38362603 (Pixiv artwork ID)
│               ├── 38362603_p0.jpg     # Page 0
│               └── 38362603_p1.jpg     # Page 1 (if multi-page)
│
├── library/                            # LIBRARY_ROOT — per-work metadata + thumbnail
│   └── {source}/                       # e.g. pixiv, iwara, x
│       └── {creator_dir}/              # From provider.get_creator_directory_name()
│           └── {source_work_id}/        # Same ID as downloads — links the two
│               ├── metadata.json       # Work metadata export
│               └── thumbnail.webp      # 400px WebP thumbnail (pyvips)
│
├── config/
│   ├── gallery-dl/                     # GALLERYDL_CONFIG_ROOT
│   │   ├── config.json                 # gallery-dl base config (extractor.pixiv.*)
│   │   ├── cookies/                    # Auth cookies per source
│   │   └── jobs/                       # Temp per-job configs (auto-cleaned)
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

- **downloads/**: gallery-dl writes files directly to its final location under `{source}/` using the naming template's directory pattern. Files are NOT moved — gallery-dl places them in the correct per-work directory. No JSON metadata files kept — they are deleted after import processing.
- **library/**: Per-work metadata + thumbnail only. Structure is `{source}/{creator_dir}/{source_work_id}/` where `creator_dir` comes from `provider.get_creator_directory_name()`. No original images.
- **gallery-dl output**: gallery-dl writes directly to `DOWNLOAD_ROOT/{source}/` using the extractor's `directory` config from the naming template. Per-work JSON metadata files are written alongside images via `--write-metadata`, then deleted by the import runner after processing.
- **Thumbnail**: 400px WebP, generated by pyvips from the first page image. Served from LIBRARY_ROOT via `/api/v1/media/thumb/{asset_id}`.
- **Preview/Original**: Served directly from DOWNLOAD_ROOT via `/api/v1/media/preview/{asset_id}` and `/api/v1/media/original/{asset_id}`. No separate preview — uses the original.
- **metadata.json**: Written per-work during import. Contains work_id, source, source_work_id, title, posted_at, creator, assets array.
- **source_work_id**: The platform-specific work ID (e.g. Pixiv artwork ID "38362603"). Used as the directory name in both downloads/ and library/. Links the two storage trees.
- **No job_id layer**: After import, downloads are organized directly under `{source}/...` — no intermediate job_id directory.
- **No JSON in downloads**: gallery-dl metadata JSON files are deleted after import processing. Use before/after snapshots to detect new files (see `_snapshot_metadata_jsons()`).

### Container path environment variables

- DOWNLOAD_ROOT=/downloads — raw downloads, original files served from here
- LIBRARY_ROOT=/library — per-work metadata + thumbnails
- GALLERYDL_CONFIG_ROOT=/gallerydl-config — gallery-dl config + cookies
- APP_CONFIG_ROOT=/app-config — application runtime config

## Security Requirements

- Do not use subprocess with shell=True.
- gallery-dl must be executed with subprocess.Popen([...], shell=False).
- Validate source URLs before creating download jobs.
- Admin APIs require authentication.
- Client must never receive real NAS filesystem paths.
- Media must be served through backend /media endpoints.
- Do not expose the system directly to the public internet in the first version.
- Multiple downloadable providers supported (Pixiv, Danbooru, Iwara, X, Pinterest, LOFTER).

## Database Requirements

Use Alembic migrations.

Use idempotent import logic.

Important uniqueness constraints:
- source_creators: unique(source, source_creator_id)
- work_sources: unique(source, source_work_id)
- asset_sources: unique(source, source_asset_id) where available
- tags: unique(normalized_name)
- subscriptions: unique(user_id, creator_id) (multiple users may subscribe to same creator)
- subscription_sources: unique(subscription_id, source)
- users: unique(username)
- albums: no cross-user uniqueness needed (user-scoped)
- album_works: unique(album_id, work_id)

Store raw source metadata in JSONB.

Never discard original metadata.

Import errors must be persisted.

## API Requirements

The API must serve both admin-web and future Flutter/mobile clients.

Use stable JSON responses.

Group routes:

- /api/v1/system
- /api/v1/sources
- /api/v1/reference
- /api/v1/creators
- /api/v1/creator-links
- /api/v1/subscriptions
- /api/v1/download-jobs
- /api/v1/import-jobs
- /api/v1/works
- /api/v1/assets
- /api/v1/tags
- /api/v1/search
- /api/v1/admin
- /api/v1/auth (added Phase 6+: login, refresh, me)
- /api/v1/me/* (added Phase 6+: user-scoped subscriptions, albums, feed)
- /api/v1/albums (added Phase 6+: album CRUD)
- /media

## Admin Web Requirements

Admin web must include:

- System health page
- Source provider management
- Danbooru reference mapping page
- Creator management page
- Creator detail page
- Manual source mapping page
- Subscription management page
- Subscription source selection
- gallery-dl configuration page
- Cookie/config status page
- Naming template management
- Download job queue
- Import job queue
- Work browser
- Work source detail
- Source tag display
- Tag manager
- Search index management
- Deduplication settings page
- Merge candidates page
- Settings page

## Development Discipline

Do not implement everything at once.

Phase order:

1. Project skeleton and Docker Compose
2. Backend foundation and database setup
3. Generic source provider abstraction
4. Creator identity model
5. Danbooru reference model and mapping workflow
6. Subscription model with selectable sources
7. Download job model and safe gallery-dl runner
8. Importer and source metadata normalization
9. Asset and tag indexing
10. Admin web foundation
11. Admin management pages
12. Deduplication settings and merge candidates
13. Documentation and NAS deployment hardening

Every major change must keep docker compose runnable.

Update README when deployment behavior changes.

## Gotchas

- **Pixiv AI detection**: `illust_ai_type=2` means AI-generated, `illust_ai_type=1` means human. Only `== 2` should flag as AI.
- **LOFTER directory**: Must use `["lofter", "{blog_name}", "{id}"]` in build_gallerydl_config. Flat directories cause all posts to merge into one work.
- **`auto_enable_on_import`**: Stored in `config.json` under each extractor, NOT in DB system_settings. Read by `reference.py` from `GALLERYDL_CONFIG_ROOT/config.json`.
- **`--write-metadata`**: Download job passes this flag. Without it, import runner has no JSON metadata to parse.
- **RQ timeout**: `job_timeout=7200` (2 hours) on all enqueue calls. RQ defaults to 180s otherwise, killing long downloads.
- **Gallery-dl exit code 0**: Does NOT guarantee files were downloaded. Always check metadata JSON count before enqueuing import.
- **`subscription_source`**: Only Pixiv defaults to `is_enabled=True` on import. Other sources default to disabled.
- **`_classify_url` in danbooru.py**: Used to determine source type for new subscription sources. Tighten regexes to avoid false matches (e.g., `www.lofter.com` must not match LOFTER pattern).

## Coding Rules

Backend:
- Keep business logic out of route handlers.
- Use services and repositories.
- Use Pydantic schemas for API responses.
- Use transactions for import operations.
- Log download/import errors into database.
- Do not let one failed image break a full import job.
- Do not implement cross-source auto-merge unless explicitly enabled.

Admin Web:
- Use typed API client.
- Use reusable components.
- Keep pages thin.
- Use TanStack Query for server state.
- Design mobile-friendly admin pages but optimize for desktop first.



## I18n Naming Convention

**CRITICAL**: All i18n keys throughout admin-web MUST use natural, human-readable names that make sense as standalone labels. Never use technical abbreviations or concatenated tech terms as keys.

### Key format
- Use `namespace.natural_name` pattern where namespace = page/feature area
- The key's value MUST be a proper Chinese/English label suitable for direct display
- Never leave a key untranslated — both `zh` and `en` values are required
- Keys that look like `jobs.download` or `common.select_all` are fine as IDs, but the Chinese VALUE must be natural language (e.g., `"下载队列"` not `"jobs.download"`)

### When adding new UI
1. First add i18n keys for ALL text that appears in the UI
2. Use Chinese values that read naturally to a Chinese speaker
3. Use English values that read naturally to an English speaker
4. Never display raw i18n key IDs — if you see a key displayed instead of its value, the key is missing from i18n.tsx

### Examples
| Key | zh (natural) | en (natural) |
|-----|-------------|---------------|
| `jobs.download` | 下载队列 | Download Queue |
| `jobs.clear_failed` | 清除失败任务 | Clear Failed |
| `jobs.no_dl` | 暂无下载任务 | No download jobs |
| `creator_detail.works_timeline` | 发布活动 | Activity |
| `gallerydl.test_connection` | 测试连接 | Test Connection |

### Anti-patterns (DO NOT USE)
- Keys whose value is the key itself (e.g., `"jobs.download": "jobs.download"`)
- Untranslated English-only values (every key must have zh + en)
- Inconsistent naming between zh and en (they should convey the same meaning)


## Frontend Modification Discipline

**CRITICAL**: When making frontend changes, do NOT add new UI elements (buttons, pages, cards, panels) unless explicitly instructed. If the user says "fix the subtitle" or "change the time picker", only modify the specific element mentioned — do not create new pages or add new navigation links.

- Before modifying any UI, confirm the exact page/component to change
- If unsure which page the user is referring to, ASK — do not guess
- When a page doesn''t exist or was deleted, inform the user and ask how to proceed
- Never add new navigation links, settings cards, or menu items without explicit instruction
- Prefix your plan with "I will modify [specific page/component] to [specific change]" before touching frontend code

### Examples
| User says | Correct response |
|-----------|-----------------|
| "fix the subtitle" | "Which page? Creator, subscription, or works?" |
| "change the time input" | "I will modify the subscription-defaults page to replace the text input with a time picker" |
| "add a test button" | Only add to the specific tab/component mentioned |

## Commit Discipline

**CRITICAL**: After completing each phase task, feature milestone, or bug fix, create a git commit automatically. Do NOT wait for the user to ask. This is mandatory.

- Commit IMMEDIATELY when a phase/sub-phase task is done
- Commit IMMEDIATELY when a bug fix is verified
- Commit before starting the next unrelated task
- Use concise Chinese or English commit messages describing the "why"
- Follow the existing commit style: `feat:`, `fix:`, `refactor:`, `chore:` prefixes
- Do not commit incomplete or broken work
- Verify working tree is clean before starting new work
- If a task spans multiple commits, create atomic commits per logical change

## Deliverables

When asked to implement, produce:
- code
- migrations
- tests where appropriate
- README update
- docker-compose update if needed (ALWAYS use `.yaml` extension, NEVER `.yml`)
- verification steps

## File Naming Conventions

- Docker Compose files: ALWAYS use `.yaml` extension (`docker-compose.yaml`), never `.yml`
- All documentation references to the compose file must use `docker-compose.yaml`

## Auto-Update Rules

These files must be kept current alongside every implementation change. Future Claude Code instances must update them when relevant:

### .gitignore
- When adding new artifact types (new language, new tool, new runtime), add corresponding patterns
- When changing data directory structure, update ignored paths
- Never un-ignore `.env` or files containing credentials

### README.md
- When deployment behavior changes (new env vars, new services, new ports)
- When supported sources change (provider goes from placeholder to downloadable)
- When setup steps change (new dependencies, new config files)
- When API route groups change

### docs/ files (English AND Chinese — must stay in sync)
- `docs/architecture.md` + `docs/architecture.zh.md`: when domain model, services, or data flow change
- `docs/setup.md` + `docs/setup.zh.md`: when installation steps, env vars, or gallery-dl config change
- `docs/development.md` + `docs/development.zh.md`: when local dev workflow, test commands, or debugging change
- `docs/providers.md` + `docs/providers.zh.md`: when adding, removing, or changing provider capabilities
- `docs/gallerydl-config.md` + `docs/gallerydl-config.zh.md`: when adding, removing, or changing per-source gallery-dl extractor config options (fields, defaults, descriptions)
- `docs/risks.md` + `docs/risks.zh.md`: when a risk materializes, is resolved, or a new risk is identified

### .claude/constraints/ and .claude/skills/
- Update when design decisions change the rules (not on every code change)
- After a risk-driven plan change, update the relevant constraint file
- `tech-stack.md`: update when adding, replacing, or removing a technology choice