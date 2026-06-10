# Architecture Audit: auto-gallery

**Date**: 2025-06-10
**Scope**: Read-only, full-repository architecture evaluation
**Method**: Static analysis of all backend (86 Python files) and frontend (60 TypeScript files) source

---

## Executive Summary

auto-gallery has a well-intentioned layered architecture (API → Service → Repository → Model) and a strong provider abstraction, but the layering is inconsistently enforced. ~40% of API routes bypass the service layer with inline SQLAlchemy queries. Two services contain near-identical domain logic (Danbooru enrichment). The import runner is a 556-line monolithic function mixing filesystem I/O, DB transactions, image processing, and search indexing. The frontend has a single 1098-line `api.ts` that bundles types, API calls, query keys, and auth.

**Overall grade: B-**. The architecture is salvageable and largely correct in intent, but needs systematic layering discipline and extraction of several overgrown modules.

---

## 1. Bounded Contexts and Module Ownership

### Current State

| Context | Owner | Files |
|---------|-------|-------|
| Creator management | CreatorService + CreatorRepository | `services/creator.py`, `repositories/creator.py` |
| Subscription management | SubscriptionService + SubscriptionRepository | `services/subscription.py`, `repositories/subscription.py` |
| Download orchestration | DownloadService + download.py job | `services/download.py`, `jobs/download.py` |
| Import processing | run_import_job (monolith) | `jobs/import_runner.py` |
| Search indexing | SearchService | `services/search.py` |
| Configuration | settings.py + settings service | `services/settings.py`, `config.py` |
| Provider abstraction | ProviderRegistry + BaseProvider | `providers/` |
| Auth | auth.py (standalone) | `auth.py` |
| Works | No service — raw repo calls | `repositories/work.py` |
| Tags | No service — raw queries in routes | `repositories/tag.py` |

### Problems

**P1. Missing WorkService and TagService**
- Evidence: [api/works.py:46-57](backend/app/api/works.py#L46-L57) calls `WorkRepository` directly. [api/works.py:87-110](backend/app/api/works.py#L87-L110) has inline SQLAlchemy queries for tags and assets.
- Impact: No caching, no validation, no transaction coordination for work/tag operations.

**P2. Ambiguous ownership of Danbooru enrichment**
- Evidence: [services/creator.py:126-185](backend/app/services/creator.py#L126-L185) and [services/subscription.py:120-176](backend/app/services/subscription.py#L120-L176) contain ~80% identical Danbooru enrichment logic.
- Impact: Bug fixes must be applied in two places. Already diverging (different logging, different exception handling).

### Target

```
CreatorService ── owns creator CRUD + source_creator management
SubscriptionService ── owns subscription CRUD + source management
WorkService (NEW) ── owns work CRUD, batch ops, asset retrieval
TagService (NEW) ── owns tag CRUD, normalization, usage counting
DanbooruEnrichmentService (NEW) ── single owner of Danbooru→creator enrichment
ImportService (NEW) ── extracted from run_import_job, coordinates import steps
DownloadRunner ── stays as RQ job, delegates to services
```

---

## 2. Dependency Direction and Layering Violations

### Current Layering

```
┌──────────────────────────────────────────┐
│  API Routes (api/)                       │
│  ✅ Some routes → Service → Repo → Model  │
│  ❌ Many routes → Raw SQLAlchemy          │
│  ❌ Some routes → Cache module directly   │
├──────────────────────────────────────────┤
│  Services (services/)                    │
│  ✅ Most → Repository → Model             │
│  ❌ settings.py reads JSON filesystem     │
│  ❌ search.py creates its own HTTP client │
├──────────────────────────────────────────┤
│  Repositories (repositories/)            │
│  ✅ Most → Model only                     │
│  ❌ creator.py calls search.py (_client)  │
├──────────────────────────────────────────┤
│  Models (models/)                        │
│  ✅ Pure SQLAlchemy ORM                  │
├──────────────────────────────────────────┤
│  Jobs (jobs/)                            │
│  ❌ import_runner.py does EVERYTHING      │
│  ❌ download.py builds configs inline     │
└──────────────────────────────────────────┘
```

### Specific Violations

**V1. API routes with inline SQLAlchemy (HIGH severity)**

| File | Lines | Content |
|------|-------|---------|
| [api/creators.py:168-242](backend/app/api/creators.py#L168-L242) | 75 lines | `get_creator_stats` — 4 inline SELECT queries with multi-table joins |
| [api/creators.py:245-368](backend/app/api/creators.py#L245-L368) | 124 lines | `get_creator_subscription_overview` — massive inline logic, provider calls, DB queries |
| [api/creators.py:392-402](backend/app/api/creators.py#L392-L402) | 11 lines | `list_source_creators` — inline SELECT |
| [api/works.py:87-110](backend/app/api/works.py#L87-L110) | 24 lines | `get_work_tags` + `get_work_assets` — inline queries with joins |
| [api/works.py:158-197](backend/app/api/works.py#L158-L197) | 40 lines | `batch_tag_works` — inline tag create/find/attach logic |
| [api/subscriptions.py:88-123](backend/app/api/subscriptions.py#L88-L123) | 36 lines | `trigger_subscription_sync` — inline DB + enqueue |
| [main.py:121-246](backend/app/main.py#L121-L246) | 126 lines | `health` endpoint — inline DB, Redis, filesystem, subprocess checks |

**V2. Repository calls search service (dependency inversion)**
- Evidence: [repositories/creator.py:26-30](backend/app/repositories/creator.py#L26-L30) calls `app.services.search._client()` directly
- Impact: Repository layer depends on infrastructure (Meilisearch HTTP client). Cannot test repository without Meilisearch.

**V3. Cache logic scattered at API layer**
- Evidence: Every list endpoint calls `cache_key()`, `cache_get()`, `cache_set()`, `cache_delete_pattern()` directly
- Impact: Cache TTLs and patterns are hard-coded in routes. No consistent caching strategy.

**V4. Service reads filesystem config**
- Evidence: [services/settings.py:75-86](backend/app/services/settings.py#L75-L86) `load_gallerydl_config()` reads a JSON file directly
- Impact: Settings service mixes DB reads with filesystem reads. Untestable without a real filesystem.

### Target Layering

```
API Routes ──→ Services ──→ Repositories ──→ Models
    │              │              │
    ✗ direct DB   ✗ direct FS   ✗ search client
    ✗ cache raw   ✗ raw Redis   
                  
API Routes: ONLY parameter parsing, service calls, HTTP exception mapping
Services: business logic, transaction coordination, cache orchestration
Repositories: pure data access, NO external service calls
Jobs: thin orchestration, delegate to services
```

---

## 3. Circular Dependencies

**No import-time circular dependencies detected.** Python module imports are acyclic. However, there are logical cycles:

**C1. CreatorService ↔ SubscriptionService (logical)**
- `CreatorService.delete_creator()` directly manipulates Subscription, SubscriptionSource, DownloadJob, ImportJob records
- `SubscriptionService._enrich_creator_from_danbooru()` calls into Danbooru service and modifies Creator records
- Each service has knowledge of the other's domain objects at the ORM level

**C2. import_runner → search → import_runner (runtime)**
- Import runner calls `SearchService._batch_index_works()` during import
- Search service reindex reads from the same tables import runner writes to
- Not a code dependency cycle, but a runtime coupling

---

## 4. Cross-Module Access (bypassing layers)

### Public vs Internal Interfaces

**Problem: All modules expose all internals**

- `services/__init__.py` — empty, no controlled exports
- `repositories/__init__.py` — empty, no controlled exports
- `jobs/__init__.py` — empty, no controlled exports
- Only `models/__init__.py` has a proper `__all__` export list

Every internal function (`_client()`, `_snapshot_metadata_jsons()`, `_detect_ai_generated()`) is importable and several are imported across module boundaries despite the `_` prefix convention.

**Specific cross-module violations:**

| Caller | Callee | Problem |
|--------|--------|---------|
| `api/creators.py:168` | `app.models.work_source.WorkSource` (direct ORM) | API bypasses service + repo |
| `api/creators.py:245` | `app.providers.registry` (direct import) | API depends on provider layer |
| `repositories/creator.py:26` | `app.services.search._client()` | Repo depends on infrastructure |
| `jobs/download.py:215` | `app.providers.registry` | Correct direction, but provider used for config building that should be in a service |
| `main.py:203` | `app.models.DownloadJob` (direct ORM) | Health check has domain model knowledge |

---

## 5. Domain Logic Leaking

### Domain logic in Infrastructure

**L1. AI/NSFW detection in import runner** (HIGH)
- Evidence: [jobs/import_runner.py:31-85](backend/app/jobs/import_runner.py#L31-L85) — `_detect_ai_generated()` and `_detect_nsfw()` are module-level functions in the import job
- These are domain rules that should live in provider-specific modules or a dedicated classification service
- 48 lines of provider-specific detection logic (Pixiv `illust_ai_type`, Danbooru `tag_string_meta`, Iwara `rating`, X `possibly_sensitive`)

**L2. MIME type detection in import runner** (LOW)
- Evidence: [jobs/import_runner.py:97-109](backend/app/jobs/import_runner.py#L97-L109) — `_mime_type()` is a standalone function
- This is a utility that could be shared but isn't hurting anything where it is

**L3. Auth error pattern matching in download job** (MEDIUM)
- Evidence: [jobs/download.py:57-65](backend/app/jobs/download.py#L57-L65) — `AUTH_ERROR_PATTERNS` is a module-level constant with regex patterns
- This is provider-specific domain knowledge. Regex patterns like `401.*unauthorized` are generic, but the interpretation ("Cookie expired", "Token revoked") is domain logic

**L4. Image dimension extraction inline** (LOW)
- Evidence: [jobs/import_runner.py:88-94](backend/app/jobs/import_runner.py#L88-L94) — `_get_image_dims()` calls pyvips directly
- Should be in a media processing service

### Domain logic in API Layer

**L5. Cache TTL decisions in routes** (MEDIUM)
- Evidence: [api/creators.py:19-20](backend/app/api/creators.py#L19-L20) `CREATOR_LIST_TTL = 300`, `CREATOR_STATS_TTL = 60`
- Cache duration is a business decision that belongs in the service layer

**L6. URL validation + provider lookup in routes** (MEDIUM)
- Evidence: [api/creators.py:292-301](backend/app/api/creators.py#L292-L301) — provider capabilities checked inline in subscription-overview
- Provider capability checks are domain logic

---

## 6. Duplicated Abstractions

**D1. Danbooru enrichment** (HIGH — exact duplicate)
- [services/creator.py:126-185](backend/app/services/creator.py#L126-L185) — `CreatorService.enrich_from_danbooru()`
- [services/subscription.py:120-176](backend/app/services/subscription.py#L120-L176) — `SubscriptionService._enrich_creator_from_danbooru()`
- Same logic: call `danbooru_svc.search_and_extract()`, upsert `danbooru_artist_id`, upsert description, create `CreatorLink` records
- Only difference: one takes `source_url/pixiv_id/artist_name`, the other takes only `source_url`

**D2. Redis client creation** (MEDIUM — scattered pattern)
- 8 files create their own `redis.from_url(settings.redis_url)` connection
- `cache.py`, `locks.py`, `import_runner.py`, `download.py`, `subscription_enqueue.py`, `main.py`, and more

**D3. `_enrich_job_context` in DownloadService** (LOW — should be in repository)
- [services/download.py:26-62](backend/app/services/download.py#L26-L62) — joins Subscription+Creator to enrich job objects
- This is a data access concern that the repository should handle

**D4. Gallery-dl config building duplicated** (MEDIUM)
- [services/settings.py:140-153](backend/app/services/settings.py#L140-L153) — `build_effective_gallerydl_config()`
- [jobs/download.py:215-267](backend/app/jobs/download.py#L215-L267) — inline config building in `run_download_job()`
- The download job rebuilds config that could be pre-built by a service

**D5. Default values duplicated** (LOW)
- [services/settings.py:15-19](backend/app/services/settings.py#L15-L19) has module-level defaults
- [api/admin.py:44](backend/app/api/admin.py#L44) has a `DEFAULT_SUB` dict with the same values

---

## 7. Obsolete or Questionable Abstractions

**O1. `jobs/batch_import.py`** — Verify if still used
- The file exists but batch import now goes through `reference.py` API + Danbooru batch import flow

**O2. Provider `parse_assets()` method** — Possibly unused in import path
- [providers/base.py:43](backend/app/providers/base.py#L43) — `parse_assets()` is defined on every provider
- But `import_runner.py` never calls it. Assets are discovered by scanning the filesystem (`work_dir.iterdir()`), not from metadata
- 10 provider implementations of this method may be dead code

**O3. `services/proxy.py`** — Thin wrapper
- Proxy config is loaded from DB and applied as env vars. The proxy module is a simple config reader — could be part of settings service

**O4. `api/__init__.py` route assembly** — Manual registration
- Each new route file requires manual editing of `api/__init__.py` to add the `include_router` call
- FastAPI supports auto-discovery patterns that would reduce this boilerplate

---

## 8. Configuration Ownership

### Three Config Sources

| Source | Owner | Mechanism | Example |
|--------|-------|-----------|---------|
| Environment variables | `config.py` (Pydantic Settings) | `os.environ` | `DATABASE_URL`, `REDIS_URL` |
| gallery-dl JSON config | `services/settings.py` | Filesystem JSON read | `config.json` extractor settings |
| System settings DB table | `services/settings.py` | SQLAlchemy reads | `subscription_defaults`, `download_defaults` |

**Problems:**

1. **Gallery-dl config is filesystem-based**: `load_gallerydl_config()` reads a JSON file that lives outside the DB. Backups must handle this separately.

2. **`auto_enable_on_import` is in gallery-dl config, not DB**: Per CLAUDE.md gotchas, this is intentional but creates a split where "source behavior" config lives in a JSON file while "system behavior" config lives in the DB.

3. **No config versioning or migration**: Changes to config structure (new extractor fields) have no migration path. The admin-web `GalleryDLMultiConfig` type must be manually kept in sync with the backend's expected JSON schema.

4. **Default values duplicated**: [services/settings.py:15-19](backend/app/services/settings.py#L15-L19) and [api/admin.py:44](backend/app/api/admin.py#L44) duplicate the same defaults.

---

## 9. Data Lifecycle and State Ownership

### Current Data Flow

```
User subscribes → subscription + subscription_source created
                → download_job created (pending)
                → RQ worker picks up download_job
                → gallery-dl subprocess downloads files + metadata JSONs
                → import_job created (pending)
                → RQ worker picks up import_job
                → import_runner creates Work, WorkSource, Asset, AssetSource, Tags
                → Meilisearch indexed
                → JSON files deleted, download_job marked complete
```

### State Ownership Issues

**S1. Job state machine is enforced but bypassable**
- [services/job_state.py](backend/app/services/job_state.py) defines valid transitions
- But [api/works.py:113-128](backend/app/api/works.py#L113-L128) `delete_work` directly calls `db.delete(work)` without going through any state machine or orphan cleanup
- Deleting a work does not clean up related AssetSource, WorkSourceTag records explicitly (relies on cascade)

**S2. Import job retry manipulates state directly**
- [jobs/import_runner.py:519-538](backend/app/jobs/import_runner.py#L519-L538) — retry logic calls `transition_import_job(ij, "pending", ...)` then re-enqueues
- The transition function is called, which is correct, but the retry strategy (1 retry, 60s backoff) is hard-coded in the job runner

**S3. No clear ownership of the "sync" state**
- `subscription_source.last_synced_at`, `last_attempted_at`, `auth_healthy`, `auth_status` are updated in 3+ places:
  - [jobs/download.py:434-444](backend/app/jobs/download.py#L434-L444) — auth health from download stderr
  - [services/subscription_enqueue.py](backend/app/services/subscription_enqueue.py) — `mark_source_sync_success`
  - Import runner — marks sync success after import

---

## 10. Concurrency and Async Boundaries

### Current State

- Backend is fully async (FastAPI + async SQLAlchemy + asyncpg)
- RQ workers are synchronous (Python `subprocess.run` is blocking)
- Image processing uses `asyncio.to_thread()` for pyvips/PIL calls

### Issues

**A1. `subprocess.run()` blocks the RQ worker event loop** (ACCEPTED — by design)
- [jobs/download.py:349](backend/app/jobs/download.py#L349) — `subprocess.run(cmd, ...)` is synchronous
- RQ workers are single-threaded sync workers, so this is correct for RQ. However, if the worker ever moves to an async model, this will be a problem.

**A2. File I/O in import runner is synchronous** (ACCEPTED — acceptable for now)
- [jobs/import_runner.py:193-194](backend/app/jobs/import_runner.py#L193-L194) — `open(jf)` and `json.load(f)` are sync
- These are small metadata JSONs, so the blocking is negligible

**A3. Redis lock uses contextmanager** (CORRECT)
- [services/locks.py:22-35](backend/app/services/locks.py#L22-L35) — proper Redis lock with Lua-based release script
- Good: uses `nx=True` for atomic acquire, Lua script for safe release

**A4. No connection pooling for Redis** (LOW)
- Every Redis operation calls `redis.from_url()` which creates a new connection pool
- `cache.py`, `locks.py`, `import_runner.py`, `download.py`, `subscription_enqueue.py` each create their own client
- In practice, `from_url()` caches pools by URL, but explicit shared client would be cleaner

---

## 11. Error Propagation

### Current Patterns (inconsistent)

| Pattern | Where | Assessment |
|---------|-------|------------|
| `ValueError` → HTTP 404 | Services (CreatorService, SubscriptionService) | ✅ Consistent |
| Swallow + log warning | Cache operations, search indexing | ⚠️ Acceptable for non-critical, but risks silent failures |
| `transition_*_job()` → DB error_log | Job state machine | ✅ Good — errors are persisted |
| Raise `HTTPException` from route | API layer | ✅ Correct pattern |
| `try/except Exception: pass` | [api/works.py:144-145](backend/app/api/works.py#L144-L145) batch_delete | ❌ Swallows all errors silently |
| Return error dict in batch results | [api/creators.py:67-80](backend/app/api/creators.py#L67-L80) | ✅ Good partial-failure pattern |

### Specific Problems

**E1. Silently swallowed errors in batch operations**
- [api/works.py:138-155](backend/app/api/works.py#L138-L155) `batch_delete_works` — `except Exception: pass`
- [api/works.py:194-195](backend/app/api/works.py#L194-L195) `batch_tag_works` — `except Exception: pass`
- No logging, no error collection, no partial results

**E2. Cache failures are silent**
- All cache operations wrap in `try/except Exception: logger.debug(...)`
- If Redis is down, the app works but has no cache — this is correct for a cache, but the `debug` level means it won't appear in normal log levels

**E3. Search indexing failures are warnings**
- All Meilisearch operations wrap in `try/except Exception: logger.warning(...)`
- Correct for non-critical search, but a completely broken search index could go unnoticed

---

## 12. Extension Points

### What Works Well

**Provider Registry** — Clean, well-designed extension point
- [providers/base.py](backend/app/providers/base.py) — clear abstract interface
- [providers/registry.py](backend/app/providers/registry.py) — simple register/get pattern
- Adding a new provider requires: create class extending BaseProvider, register in registry, done
- 10 providers implemented, all following the same pattern

### What's Missing

**X1. No plugin/event hooks for import pipeline**
- Import runner has fixed steps: parse → create records → thumbnails → pHash → tags → metadata.json → Meilisearch
- A plugin wanting to add a step (e.g., "upload to S3") has no hook to attach to
- Suggestion: import pipeline events (`on_work_created`, `on_asset_created`, `on_import_complete`)

**X2. No service interface abstractions**
- `CreatorService`, `SubscriptionService`, `DownloadService` are concrete classes
- Swapping implementations (e.g., for testing) requires mocking at the class level
- No `Protocol` or ABC for services

**X3. Provider capabilities are not extensible**
- `ProviderCapabilities` is a fixed dataclass with 5 boolean fields
- Adding a new capability (e.g., `supports_live_streaming`) requires changing the base class

---

## 13. Deployment Boundaries

### Current State

| Service | Role | Image | Correct? |
|---------|------|-------|----------|
| backend | FastAPI API server | backend image | ✅ |
| worker | RQ worker (downloads + imports) | same backend image | ✅ |
| scheduler | RQ scheduler (subscription sync) | same backend image | ✅ |
| admin-web | Next.js frontend | separate image | ✅ |
| postgres | Database | postgres:16 | ✅ |
| redis | Queue + cache | redis:7 | ✅ |
| meilisearch | Search | meilisearch | ✅ |

**Boundary issues:**

1. **Worker runs both downloads and imports in the same process**: RQ queues ("downloads" and "imports") are separate, but the worker process handles both. A stuck download won't block imports (different RQ workers), but both share the same Python process memory.

2. **Backend image includes gallery-dl but shouldn't use it**: Per CLAUDE.md, "backend API processes must not execute gallery-dl directly." The constraint prevents the API from calling gallery-dl, but the binary is present in the image. No runtime enforcement — relies on developer discipline.

3. **No health check separation**: The health endpoint in main.py checks postgres + redis + meilisearch + disk + queues + jobs + gallery-dl. This is useful for debugging but means the health endpoint does significant work on every call.

---

## 14. Frontend Architecture

### Current State

| Layer | Files | Assessment |
|-------|-------|------------|
| Pages | `app/admin/**/page.tsx` (37 pages) | Mostly thin, use TanStack Query ✅ |
| Components | `components/*.tsx` (14 shared) | Clean, reusable ✅ |
| API Client | `lib/api.ts` (1098 lines) | Too large ❌ |
| Auth | `lib/auth.tsx` | Clean, context-based ✅ |
| i18n | `lib/i18n.tsx`, `lib/i18n-format.ts` | Well-structured ✅ |
| Theme | `lib/theme.tsx` | Clean ✅ |
| Hooks | `lib/useDebounce.ts` | Only one shared hook ⚠️ |

### Issues

**F1. api.ts is monolithic** (HIGH)
- [admin-web/src/lib/api.ts](admin-web/src/lib/api.ts) — 1098 lines
- Contains: all TypeScript types (66 interfaces), all API functions (~110 methods), query key factory, auth functions
- Should be split: `api/types.ts`, `api/creators.ts`, `api/subscriptions.ts`, `api/client.ts`, etc.

**F2. Manual type synchronization**
- Backend Pydantic schemas and frontend TypeScript interfaces are maintained independently
- No code generation, no OpenAPI-based type extraction
- Risk: frontend types drift from backend responses

**F3. Error handling is inconsistent**
- Some pages use ErrorState component, some have inline error handling
- No global error boundary strategy documented

**F4. Missing shared hooks**
- Only `useDebounce` in shared hooks
- Common patterns like `useCreators`, `useSubscriptions` are duplicated across pages as inline `useQuery` calls

---

## 15. Proposed Target Architecture (Evolutionary)

### Phase 1: Fill Service Layer Gaps (Low Risk)

**1a. Create WorkService**
- **Current problem**: `api/works.py` calls `WorkRepository` directly and has inline SQLAlchemy for tags/assets
- **Concrete evidence**: [api/works.py:46-57](backend/app/api/works.py#L46-L57), [api/works.py:87-110](backend/app/api/works.py#L87-L110), [api/works.py:158-197](backend/app/api/works.py#L158-L197)
- **Target boundary**: `WorkService` class in `services/work.py` with methods: `list_works()`, `get_work()`, `get_work_tags()`, `get_work_assets()`, `delete_work()`, `batch_delete_works()`, `batch_tag_works()`, `toggle_favorite()`
- **Migration order**: Create service → update routes one by one → verify each
- **Compatibility**: API responses unchanged. Same schemas, same status codes.
- **Rollback**: Revert routes to inline queries. No DB migration needed.
- **Verification**: `docker compose exec backend python -m pytest`, manual smoke test of works page
- **Estimated risk**: Low

**1b. Create TagService**
- **Current problem**: Tag operations scattered across `api/works.py`, `api/tags.py`
- **Concrete evidence**: [api/works.py:158-197](backend/app/api/works.py#L158-L197) inline tag create/find/attach
- **Target boundary**: `TagService` class in `services/tag.py`
- **Migration order**: Create service → update tag routes → update work batch-tag route
- **Compatibility**: API responses unchanged
- **Rollback**: Revert to inline queries
- **Verification**: Tag CRUD operations via admin web
- **Estimated risk**: Low

**1c. Move health endpoint logic**
- **Current problem**: [main.py:121-246](backend/app/main.py#L121-L246) contains 126 lines of health check logic in the app factory
- **Target boundary**: `services/health.py` with `HealthService` that the route delegates to
- **Migration order**: Create HealthService → update main.py to call it
- **Compatibility**: Same endpoint, same response shape
- **Rollback**: Move logic back to main.py
- **Verification**: `curl /api/v1/system/health` returns identical JSON
- **Estimated risk**: Low

### Phase 2: Extract Duplicated Logic (Low-Medium Risk)

**2a. Create DanbooruEnrichmentService**
- **Current problem**: ~80% duplicate enrichment logic in two services
- **Concrete evidence**: [services/creator.py:126-185](backend/app/services/creator.py#L126-L185) and [services/subscription.py:120-176](backend/app/services/subscription.py#L120-L176)
- **Target boundary**: `DanbooruEnrichmentService` in `services/danbooru_enrichment.py` with single `enrich_creator(creator_id, source_url, pixiv_id, artist_name)` method
- **Migration order**: Create shared service → update CreatorService → update SubscriptionService → remove duplicates
- **Compatibility**: Both callers produce identical results
- **Rollback**: Revert each caller to its own implementation
- **Verification**: Import Danbooru artist → same links created. Both call paths tested.
- **Estimated risk**: Low

**2b. Extract AI/NSFW detection to providers**
- **Current problem**: 48 lines of provider-specific detection logic in import runner
- **Concrete evidence**: [jobs/import_runner.py:31-85](backend/app/jobs/import_runner.py#L31-L85) — `_detect_ai_generated()` and `_detect_nsfw()`
- **Target boundary**: Add `detect_ai_generated(raw_metadata) -> bool` and `detect_nsfw(raw_metadata) -> bool` to `BaseProvider` with default `False` implementations. Each provider overrides.
- **Migration order**: Add methods to BaseProvider → implement in Pixiv, Danbooru, Iwara, X → update import_runner to call provider methods → remove module-level functions
- **Compatibility**: Import results identical. AI/NSFW flags unchanged.
- **Rollback**: Restore module-level functions, remove provider methods
- **Verification**: Import known AI work from Pixiv (illust_ai_type=2), verify `is_ai_generated=True`. Import known R-18 work, verify `is_nsfw=True`.
- **Estimated risk**: Medium (touches import pipeline)

### Phase 3: Split Monolithic Files (Medium Risk)

**3a. Refactor import_runner.py**
- **Current problem**: 556-line monolithic function mixing filesystem I/O, DB transactions, image processing, search indexing
- **Concrete evidence**: [jobs/import_runner.py](backend/app/jobs/import_runner.py) — single `run_import_job()` function
- **Target boundary**:
  - `services/import_service.py` — orchestration: `import_work()` calls sub-steps
  - `services/classification.py` — AI/NSFW detection (if not moved to providers in 2b)
  - `services/library_writer.py` — metadata.json writing
  - `jobs/import_runner.py` — thin entry point that delegates to ImportService
- **Migration order**: Extract library_writer → extract classification → extract orchestration → thin out job entry point
- **Compatibility**: `run_import_job(import_job_id)` function signature preserved as RQ entry point
- **Rollback**: Revert to monolith if extraction causes issues
- **Verification**: Run import on test data, verify all records (Work, WorkSource, Asset, AssetSource, Tags, thumbnails, metadata.json, Meilisearch) created correctly
- **Estimated risk**: Medium (core pipeline)

**3b. Split frontend api.ts**
- **Current problem**: 1098-line monolithic file
- **Concrete evidence**: [admin-web/src/lib/api.ts](admin-web/src/lib/api.ts)
- **Target boundary**:
  - `lib/api/types.ts` — all TypeScript interfaces
  - `lib/api/client.ts` — `request()`, `ApiError`, `queryKeys`
  - `lib/api/endpoints/creators.ts`, `subscriptions.ts`, `works.ts`, etc.
  - `lib/api/index.ts` — re-exports `api` object with identical shape
- **Migration order**: Extract types → extract client → extract endpoints one domain at a time → re-export
- **Compatibility**: `import { api } from "@/lib/api"` works identically
- **Rollback**: Restore single file
- **Verification**: `npm run build` succeeds, all pages load without import errors
- **Estimated risk**: Low

### Phase 4: Strengthen Boundaries (Medium Risk)

**4a. Remove search dependency from repository**
- **Current problem**: Repository imports search infrastructure
- **Concrete evidence**: [repositories/creator.py:26-30](backend/app/repositories/creator.py#L26-L30) calls `app.services.search._client()`
- **Target boundary**: `CreatorRepository.list_all()` only queries the DB. Search enrichment moves to `CreatorService.list_creators()`.
- **Migration order**: Add search enrichment in service → remove from repository → verify search still works
- **Compatibility**: Creator search with Meilisearch fallback continues to work
- **Rollback**: Restore search call in repository
- **Verification**: Search for creators with Meilisearch available and unavailable
- **Estimated risk**: Medium (changes query flow)

**4b. Add shared Redis connection**
- **Current problem**: 8 files create independent Redis connections
- **Concrete evidence**: `cache.py`, `locks.py`, `import_runner.py`, `download.py`, `subscription_enqueue.py`, `main.py`, `job_manifest.py`, `log_buffer.py`
- **Target boundary**: `services/redis_client.py` with `get_redis()` function
- **Migration order**: Create shared client → update one module at a time → verify
- **Compatibility**: No behavioral change
- **Rollback**: Revert each module to direct `from_url` call
- **Verification**: All Redis operations continue to work (cache, locks, queue, manifest)
- **Estimated risk**: Low

**4c. Add `__all__` exports to all packages**
- **Current problem**: No controlled public API for services, repositories, jobs packages
- **Target boundary**: Each `__init__.py` exports only intended public interfaces
- **Migration order**: Add exports one package at a time
- **Compatibility**: Existing imports may need adjustment if they import private symbols
- **Rollback**: Remove `__all__` restrictions
- **Verification**: `python -c "from app.services import *"` works correctly
- **Estimated risk**: Very low

### Phase 5: API Contract Stabilization (Medium-High Risk)

**5a. Generate TypeScript types from Pydantic schemas**
- **Current problem**: Manual type synchronization between backend and frontend
- **Concrete evidence**: [admin-web/src/lib/api.ts:38-581](admin-web/src/lib/api.ts#L38-L581) — 66 hand-maintained TypeScript interfaces
- **Target boundary**: OpenAPI schema from FastAPI → codegen → `lib/api/types.generated.ts`
- **Migration order**: Set up OpenAPI extraction → generate types → compare with manual types → adopt generated types
- **Compatibility**: Generated types replace manual ones. API client functions use generated types.
- **Rollback**: Keep manual types as fallback
- **Verification**: `npm run build` with generated types produces no type errors
- **Estimated risk**: High (tooling setup, may reveal type mismatches)

### Migration Order (Dependency Graph)

```
Phase 1a → 1b → 1c  (independent, can parallelize)
    ↓
Phase 2a → 2b        (2a cleans up before 3a touches same code)
    ↓
Phase 3a → 3b        (3a is riskier, do first; 3b is independent)
    ↓
Phase 4a → 4b → 4c  (4a is riskiest, do first; 4b+4c are mechanical)
    ↓
Phase 5a             (last — depends on stable backend types)
```

---

## Summary Risk Matrix

| # | Issue | Severity | Effort | Risk |
|---|-------|----------|--------|------|
| V1 | Inline SQLAlchemy in 7 route handlers | HIGH | Medium | Low |
| D1 | Duplicate Danbooru enrichment logic | HIGH | Small | Low |
| L1 | AI/NSFW detection in import runner | HIGH | Medium | Medium |
| V2 | Repository depends on search service | MEDIUM | Medium | Medium |
| F1 | Monolithic api.ts (1098 lines) | MEDIUM | Medium | Low |
| V3 | Cache orchestration at API layer | MEDIUM | Medium | Low |
| L5 | Domain logic in health endpoint | MEDIUM | Small | Low |
| D2 | Scattered Redis client creation | LOW | Small | Low |
| S1 | Work deletion bypasses state machine | MEDIUM | Small | Medium |
| E1 | Silently swallowed batch errors | MEDIUM | Small | Low |
| S3 | Sync state updated from 3+ locations | MEDIUM | Medium | Medium |
| X1 | No import pipeline hooks | LOW | Large | High |
| F2 | Manual type synchronization | MEDIUM | Large | High |
| O2 | Provider parse_assets() possibly unused | LOW | Small | Low |
| D5 | Duplicate default values | LOW | Small | Low |

---

## Verification Methods (General)

1. **Run existing test suite**: `docker compose exec backend python -m pytest`
2. **API response comparison**: Snapshot API responses before/after each phase
3. **Integration test**: Import a known Pixiv work through the full pipeline
4. **Frontend build**: `docker compose build admin-web && docker compose up -d admin-web`
5. **Manual smoke test**: Navigate admin-web pages after each phase
