# auto-gallery Project Guide

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
- [tech-stack.md](../.claude/constraints/tech-stack.md) — full technology stack with versions and exclusions
- [source-abstraction.md](../.claude/constraints/source-abstraction.md) — generic model naming, provider interface
- [danbooru-reference.md](../.claude/constraints/danbooru-reference.md) — Danbooru as reference only
- [deduplication.md](../.claude/constraints/deduplication.md) — dedup policy, opt-in only
- [docker.md](../.claude/constraints/docker.md) — services, container paths, volume mapping
- [security.md](../.claude/constraints/security.md) — subprocess, URL validation, auth (admin + JWT), CORS, /media
- [client-api.md](../.claude/constraints/client-api.md) — user model, client-facing APIs, albums, mobile response design
- [remote-access.md](../.claude/constraints/remote-access.md) — LAN/VPN/reverse-proxy, HTTPS, network architecture

Read constraints before implementing. The risk register documents decisions made during architecture review.

## Risk-Derived Decisions

The following were decided during risk analysis (see [docs/risks.md](../docs/risks.md) for rationale):

- **RQ for job queue** (not Celery). Simpler, uses Redis already in stack. `download_job`/`import_job` tables are source of truth; queue backend is replaceable.
- **`creator_link.confidence` field** (0.0–1.0 float). Suggested links from Danbooru or URL extraction are scored. Admin reviews low-confidence suggestions.
- **Job state machine**: `pending → downloading → downloaded → importing → complete | failed | stale`. Worker startup reconciles orphaned DOWNLOAD_ROOT directories.
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

Danbooru is a reference provider in the current phase.

Danbooru artist tag data may be used to:
- fetch or store artist reference records
- extract related URLs
- suggest creator links
- suggest source account mappings
- enrich creator identity data

Danbooru must not be assumed to contain the complete union of all works from all source platforms.

Do not make Danbooru the default media download source.

Do not assume that downloading from Danbooru is enough to archive a creator.

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

First implementation:
- actual download support may be Pixiv only
- X and Iwara should be placeholders
- Danbooru artist reference import should be implemented separately from media download

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
- backend/app/providers/danbooru_reference.py
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

## NAS Paths

Do not hard-code /volume1 in application code.

Application code must only use environment variables and container paths:

- DOWNLOAD_ROOT=/downloads
- LIBRARY_ROOT=/library
- APP_CONFIG_ROOT=/app-config
- GALLERYDL_CONFIG_ROOT=/gallerydl-config

Docker Compose maps these to NAS paths.

## Security Requirements

- Do not use subprocess with shell=True.
- gallery-dl must be executed with subprocess.Popen([...], shell=False).
- Validate source URLs before creating download jobs.
- Admin APIs require authentication.
- Client must never receive real NAS filesystem paths.
- Media must be served through backend /media endpoints.
- Do not expose the system directly to the public internet in the first version.
- First downloadable provider can be Pixiv only.
- Iwara and X are placeholders until explicitly implemented.

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

## Deliverables

When asked to implement, produce:
- code
- migrations
- tests where appropriate
- README update
- docker-compose update if needed
- verification steps

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
- `docs/risks.md` + `docs/risks.zh.md`: when a risk materializes, is resolved, or a new risk is identified

### .claude/constraints/ and .claude/skills/
- Update when design decisions change the rules (not on every code change)
- After a risk-driven plan change, update the relevant constraint file
- `tech-stack.md`: update when adding, replacing, or removing a technology choice