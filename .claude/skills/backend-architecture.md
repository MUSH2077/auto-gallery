# Backend Architecture Skill

Guidance for implementing backend code that follows the project's layered architecture and constraint rules.

## Layer structure

```
Route handler (thin)
  → Service (business logic)
    → Repository (data access)
      → SQLAlchemy model
```

Routes must never contain business logic or direct database access. Services orchestrate operations. Repositories encapsulate queries.

## File layout

```
backend/
  app/
    main.py              # FastAPI app, lifespan, middleware
    config.py            # Pydantic Settings from env vars
    database.py          # engine, sessionmaker, get_db dependency
    models/
      __init__.py
      base.py            # declarative base, mixins (TimestampMixin)
      creator.py
      source_creator.py
      creator_link.py
      work.py
      work_source.py
      asset.py
      asset_source.py
      tag.py
      work_tag.py
      work_source_tag.py
      subscription.py
      subscription_source.py
      download_job.py
      import_job.py
      naming_template.py
    schemas/
      __init__.py
      # Pydantic response/request schemas
    repositories/
      __init__.py
      # One repository per aggregate root
    services/
      __init__.py
      # Business logic layer
    api/
      __init__.py
      router.py          # main api router
      deps.py            # dependency injection (get_db, get_current_user)
      system.py
      sources.py
      reference.py
      creators.py
      creator_links.py
      subscriptions.py
      download_jobs.py
      import_jobs.py
      works.py
      assets.py
      tags.py
      search.py
      admin.py
      media.py
    providers/
      __init__.py
      base.py            # AbstractProvider
      registry.py        # ProviderRegistry
      pixiv.py
      iwara.py
      x.py
      danbooru_reference.py
      local.py
      manual.py
    jobs/
      __init__.py
      download.py        # gallery-dl runner (worker only)
      import.py           # metadata import pipeline
  alembic/
    env.py
    versions/
  tests/
```

## Model conventions

- Every model gets `id: UUID` primary key
- Every model gets `created_at`, `updated_at` via TimestampMixin
- Source-specific raw metadata stored as `raw_metadata: JSONB`
- Uniqueness constraints at database level, not just application logic
- Foreign keys use `ON DELETE` appropriate to the relationship

### Risk-derived model fields (non-negotiable)

From risk analysis (see `docs/risks.md`):

**`creator_link`**:
- `confidence: Float` (0.0–1.0). Suggested links from Danbooru/URL parsing are scored. Manual bindings default to 1.0. Admin reviews low-confidence links.

**`download_job`**:
- `status: String` must follow the state machine: `pending | downloading | downloaded | complete | failed | stale | paused`
- `error_log: Text` captures gallery-dl stderr on failure
- `retry_count: Integer` defaults to 0, max 3
- See `.claude/skills/gallerydl-integration.md` for the canonical state machine with transitions

**`subscription_source`**:
- `last_successful_auth: DateTime` for cookie expiration visibility
- `auth_healthy: Boolean` defaults to True, set False on auth-related failures

**`import_job`**:
- `status: String`: `pending | running | complete | failed` (separate lifecycle from download_job)
- `error_log: Text` captures per-asset import failures
- Auto-retry: first failure re-enqueues as pending with 60s backoff; second failure is permanent
- Must be idempotent — re-running must not create duplicates

### Meilisearch sync

v1: Admin-triggered full re-indexing via `POST /api/admin/search/reindex`.
v2: Application-level dual-writes in create/update/delete paths.

### gallery-dl smoke test (required)

A smoke test that validates gallery-dl output format compatibility:
- Run `gallery-dl --write-info-json` against a known-good URL
- Parse the output `.info.json`
- Assert that all fields expected by `parse_work_source()` are present
- Run this before upgrading gallery-dl version in requirements.txt

## API conventions

- Route prefix: `/api/v1/` for all endpoints (versioned from day one)
- Media serving: `/media/` with controlled file access (unversioned, stable)
- All responses are JSON via Pydantic schemas
- Pagination: cursor-based for lists, offset where cursor is impractical; default 50 items, max 200
- Errors: consistent `{"detail": "...", "error_code": "..."}` format

## Migration safety

Alembic `--autogenerate` can detect most changes but must be reviewed:

- Always inspect the generated migration before committing
- Watch for: dropped tables/columns, altered constraints, renamed columns (autogenerate treats rename as drop+add)
- If a migration drops data, it must be explicitly justified in the commit message
- Test `alembic upgrade head` and `alembic downgrade -1` before merging
- Migrations run via `docker compose run --rm backend alembic upgrade head` before app start

## Logging strategy

- Structured logging via `structlog` or standard `logging` with JSON format
- Log levels: DEBUG (local dev), INFO (default), WARNING, ERROR
- All download/import errors must be persisted to DB job records, not just logs
- Worker logs must include `job_id` and `source` context
- NAS deployments: logs go to stdout/stderr, captured by Docker, viewable via `docker compose logs`

## Startup checks

On backend startup, before accepting requests:
- Verify database connectivity
- Verify Redis connectivity (if used by API for caching)
- Do NOT verify Meilisearch (it may start slower; treat as degraded, not fatal)
- Return health status at `GET /api/v1/system/health`

## Provider resolution

```python
# registry.py pattern
registry = ProviderRegistry()
registry.register(PixivProvider())
registry.register(IwaraProvider())
# ...

provider = registry.get("pixiv")
```

Use `registry.get(source_name)` to resolve a provider. Never import provider classes directly outside the registry module.
