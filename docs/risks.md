# Risk Register

## HIGH Severity

### 1. Multi-user data model transition

**Risk**: The current Phase 1-5 design has no user model. Adding it in Phase 6 requires:
- Schema migration: add `user_id` to `subscription` table
- API breaking change: all subscription endpoints change scope from global to per-user
- Auth migration: from API key to JWT
- Data migration: existing subscriptions must be assigned to a default admin user

**Impact**: If not planned for, Phase 6 becomes a major refactor instead of an addition. Existing API consumers (admin-web) break.

**Mitigation**:
- Design Phase 1-5 with awareness that `user_id` will be added to subscription
- Keep subscription endpoints behind admin routes until Phase 6 (avoid premature public API)
- Use repository pattern so data access layer changes are localized
- Reserve the `user_id` column name; don't use it for anything else
- Write Phase 6 migration as: add nullable `user_id`, backfill with default admin, make non-nullable
- Admin-web built against `/api/v1/admin/*` routes (unchanged by user model addition)

**Decision**: Phase 1-5 stays admin-only. Phase 6 adds user model + migration path. No premature user model.

---

### 2. gallery-dl version / output format stability

**Risk**: gallery-dl updates frequently. Pinning a version means site-specific extractors may break when source sites change. Not pinning means output format changes can silently break metadata parsers.

**Impact**: Broken downloads, failed imports, missing metadata

**Mitigation**:
- Pin a known-good gallery-dl version in `requirements.txt`
- Add integration smoke tests: run gallery-dl against a known URL, verify JSON output structure matches parser expectations
- When upgrading gallery-dl, run the smoke test suite first
- Version the gallery-dl config schema

**Decision**: Pin version + smoke tests. Do not auto-upgrade.

---

### 3. Cross-source creator identity mapping

**Risk**: Linking accounts across platforms ("Pixiv user 123456" <-> "@artist_handle on X") is fundamentally hard. Profile URLs are embedded in free-text fields. Many creators use completely different names. Automation will have limited accuracy.

**Impact**: Manual mapping burden on admin. Fragmented creator records.

**Mitigation**:
- Accept that automatic mapping will be ~60% accurate at best
- Start with manual mapping as the primary path
- Danbooru reference enrichment is a suggestion engine, not an authority
- Add `creator_link.confidence` field (0.0-1.0) for suggested links
- Admin review queue for suggested links
- Do NOT make automatic mapping a dependency for Phases 1-6

**Decision**: Manual-first with suggestion engine. Architecture handles this.

---

### 4. DOWNLOAD_ROOT -> LIBRARY_ROOT atomicity

**Risk**: gallery-dl writes to `DOWNLOAD_ROOT/<job_id>/`. Import moves files to `LIBRARY_ROOT/`. If the worker crashes between these steps:
- Files in DOWNLOAD_ROOT with no import job (orphaned downloads)
- DB records pointing to files that failed to move (broken references)

**Impact**: Storage waste, broken media URLs, inconsistent database

**Mitigation**:
- Job state machine: `pending -> downloading -> downloaded -> importing -> complete`
- On worker startup, scan DOWNLOAD_ROOT for orphaned directories, reconcile with `download_job` table
- Import must be fully idempotent: re-running with same files creates no duplicates
- Use `(source, source_asset_id)` uniqueness to prevent duplicate asset records
- File moves within the same filesystem (same Docker volume) are atomic if using `os.rename`

**Decision**: State machine + startup reconciliation + idempotent imports.

---

## MEDIUM Severity

### 5. Meilisearch index consistency

**Risk**: PostgreSQL and the asynchronous Meilisearch projection can drift or
the projection can lag while Meilisearch is unavailable.

**Mitigation**:
- PostgreSQL writes and a coalescing projection-outbox intent share one
  transaction; ordinary imports never wait for Meilisearch.
- A single bounded consumer applies at most one Meilisearch write task at a
  time, with retry/backoff and observable lag.
- Full rebuilds use versioned staging indexes and keyset batches, then replay
  outbox changes made after the rebuild watermark before an atomic swap.
- Structured work lists remain available from PostgreSQL while full-text search
  returns an explicit `503 search_unavailable` during a search outage.

---

### 6. Provider abstraction leak

**Risk**: Pixiv metadata (series, caption structure) differs significantly from Iwara (voice actors, 3D tags), X (hashtags, thread context), Danbooru (tag categories), and Weibo (hashtag patterns). Forcing all through identical `parse_*` methods may produce awkward lowest-common-denominator schemas.

**Mitigation**:
- `raw_metadata` JSONB column is the escape hatch -- everything source-specific lives there
- Typed fields capture only what maps cleanly
- Admin web may have provider-specific components that render `raw_metadata` per source
- Provider `parse_*` methods extract best-effort typed data; nothing is discarded

---

### 7. Single worker bottleneck

**Risk**: One worker processes one job at a time. A creator with 1000+ works takes hours/days to fully sync.

**Decision**: ACCEPTED for v1 (personal archive, not real-time service).
The 8 GiB NAS profile intentionally caps effective download concurrency at one.
Do not scale workers by hand: resource-profile leases, per-source archive locks,
staging manifests, and the fair scheduler are the supported way to add future
parallelism after mixed-load validation.

---

### 8. Multi-user storage sharing

**Risk**: Multiple users subscribing to the same creator would download the same files repeatedly if data isn't shared. Creator/Work/Asset must be global, not per-user. But this creates a privacy consideration: one user's subscription reveals the creator to all users.

**Mitigation**:
- `creator`, `work`, `asset`, `tag` tables are global (shared, no user_id)
- `subscription`, `album`, `download_job` tables are user-scoped (have user_id)
- Download once, reference many times via `asset_sources`
- Creator list is public to all authenticated users (acceptable for a personal/small-group archive)
- If private creators are needed later: add `creator.visibility` field

**Decision**: Shared creator/work/asset data model. User-scoped subscriptions and albums only.

---

### 9. Remote client bandwidth and image loading

**Risk**: Flutter client loading full-resolution images over mobile data will be slow and consume excessive bandwidth.

**Mitigation**:
- Generate thumbnails at import time: 200px, 600px, 1200px widths
- Client API returns thumbnail URLs alongside full-resolution URLs
- Client fetches thumbnails for grid/list views, full image only on detail view
- Add `?width=` query param to `/media/` for dynamic resizing (future, pyvips can do this)
- Cache-Control headers on media: long max-age for immutable assets

**Decision**: Multi-size thumbnails generated at import. No on-the-fly resizing in v1.

---

### 10. Client auth token lifecycle

**Risk**: JWT access tokens expire every 15 minutes. Flutter client must transparently refresh. If refresh logic is buggy, user sees auth errors. If refresh token expires (30 days), user must re-login. Remote re-login when not on LAN is impossible without VPN.

**Mitigation**:
- Flutter HTTP interceptor (dio) handles 401 -> refresh -> retry transparently
- Refresh token stored in platform secure storage
- On refresh failure: clear tokens, redirect to login screen
- Refresh token expiry: show notification 7 days before expiry
- Admin can issue long-lived access tokens for trusted clients (future)

**Decision**: Standard JWT access+refresh pattern. dio interceptor for transparent refresh.

---

### 11. NAS HDD I/O performance

**Risk**: Gallery-dl downloads and image processing (thumbnail generation, hash computation) are I/O-intensive. On a spinning-disk NAS, initial sync of a large creator may saturate the disk.

**Mitigation**:
- Generate thumbnails at import time, not on first read request
- Accept that initial sync of a large creator will be slow
- pyvips for efficient thumbnail generation (faster, lower memory than Pillow)
- Worker runs in background; UI is not blocked by slow I/O

**Decision**: Accept initial sync latency. pyvips for efficiency.

---

## RESOLVED

Items below were risks that have been addressed by implementation.

### Risk A: X/Twitter API viability (EXPLICITLY DISABLED)

**Original concern**: X had aggressively restricted API access. gallery-dl's X extractor may break permanently or require paid API access.

**Status**: X/Twitter is explicitly disabled (`can_download=False`, `supports_gallerydl=False`). The provider exists as a placeholder with URL validation and tag parsing, but the download pipeline will not create jobs for X sources. The UserTweets GraphQL endpoint and SearchTimeline fallback are both known-unreliable. Re-evaluate after the Pixiv pipeline is stable. The Dockerfile still patches the SearchTimeline fallback for when the provider is re-enabled.

---

### Risk B: Queue backend selection (CONFIRMED)

**Original concern**: RQ vs Celery was undecided. RQ is simpler but has fewer failure-recovery features.

**Resolution**: RQ selected and in production use. Uses Redis (already in stack), simpler config. Job model (`download_job`/`import_job`) abstracts the queue. `job_timeout=7200` on all enqueue calls. Migration to Celery remains feasible if needed.

---

### Risk C: gallery-dl auth/cookie expiration (RESOLVED)

**Original concern**: Pixiv session cookies expire. Failed auth results in silent download failures.

**Resolution**: Implemented with:
- `auth_healthy` boolean on `subscription_source` tracked per-source
- `last_successful_auth` timestamp on `subscription_source`
- Auth Status page at **Settings -> Auth Status** showing health per source
- Connectivity test endpoint `POST /admin/gallerydl-config/test-connection` available per source at **Settings -> gallery-dl Config**
- Stale job detection: jobs stuck "downloading" for > 2x timeout are marked stale
- Health endpoint detects sources with recent auth failures
- Admin web shows auth health indicators per subscription source

---

### Risk D: Iwara gallery-dl support (RESOLVED)

**Original concern**: Unknown whether gallery-dl supports Iwara. If not, Iwara would remain a placeholder.

**Resolution**: Iwara is now a fully working downloadable provider with `can_download=True`, `supports_gallerydl=True`, `supports_tags=True`. Supports video and image downloads with configurable format quality preferences (Source, 1080, 720, etc.). Both username/password and cookie authentication supported.

---

### Risk E: Admin auth mechanism (RESOLVED)

**Original concern**: Need to decide on admin auth before Phase 6.

**Resolution**: JWT authentication with access + refresh token pattern fully implemented. Backend: `auth.py` with `create_access_token`/`decode_access_token`, `auth_api.py` with login/refresh/me endpoints, admin routes require Bearer JWT. Admin-web: `AuthProvider` context in `auth.tsx` wraps the app, transparent token refresh, localStorage persistence, login page with username/password.

---

### Risk F: Schedule tolerance and restart guards (RESOLVED)

**Original concern**: On container restart, the scheduler could enqueue duplicate sync jobs if it re-scans all subscriptions before realizing they were already scheduled.

**Resolution**:
- `seed_sync.py` checks for existing `sync_subscriptions` jobs in the "scheduled" RQ queue before enqueuing. Only bootstraps if no existing job is found.
- The scheduler uses a `+-scan_interval/2` tolerance window: a subscription is only synced if the current time falls within half the scan interval of a scheduled time slot.
- The `_should_sync_now()` function checks whether the last sync occurred before the scheduled time slot, preventing re-trigger of already-completed syncs within the same window.
- Stale job detection (2x timeout) prevents zombie jobs from blocking subsequent syncs.
- Catch-up logic: if last sync was > 24h ago, triggers a one-off catch-up sync regardless of time window.

---

### Risk G: Timezone-aware scheduling (RESOLVED)

**Original concern**: The scheduler needed to respect the NAS server's local timezone for fixed-time schedules (e.g. "sync at 2 AM daily").

**Resolution**:
- `TIMEZONE` configuration in `subscription_defaults` (system_settings table), default `"UTC"`.
- Per-deployment override: admin can change the timezone in **Settings -> Subscription Defaults**.
- `ZoneInfo`-based timezone parsing with fallback to UTC on invalid tz names.
- All schedule time comparisons (`scheduled_times`, `last_synced_at`) are evaluated in the configured timezone.
- Works for all IANA timezone identifiers (e.g. `"Asia/Shanghai"`, `"America/New_York"`).

---

### Risk H: Dockerfile security patches (RESOLVED)

**Original concern**: gallery-dl's X/Twitter extractor included a SearchTimeline fallback that was known to fail with 401 errors, potentially masking real auth issues.

**Resolution**: Dockerfile applies a sed patch that removes the SearchTimeline fallback from gallery-dl's twitter extractor module. Only the UserTweets GraphQL endpoint is used.

---

## LOW Severity

### L1. Alembic migration execution

Compose runs `alembic upgrade head` in the one-shot `migrate` service. Backend
and workers wait for that service to complete successfully, so ordinary
container restarts no longer rerun migrations in a crash loop.

### L2. File naming / reorganization

gallery-dl names files during download. Import job can reorganize. Asset paths are stored in DB after import, not before. Changes to gallery-dl file organization affect future downloads without rewriting imported asset paths.

### L3. Danbooru phase ordering

Creator identity (Phase 4) works without Danbooru (Phase 5). Danbooru enriches identity, doesn't enable it. Correctly ordered.

### L4. gallery-dl archive database

gallery-dl's SQLite archive files track already-downloaded URLs. Maintenance
runs at 03:30: a lightweight `PRAGMA optimize` daily and full `VACUUM` only on
Sunday while queues are idle, pressure is normal, and the global heavy-I/O lock
is held. This lowers, but does not eliminate, the maintenance-window I/O risk.

### L5. Provider count growth

As the number of providers grows (currently 8: Pixiv, X, Iwara, Danbooru, Pinterest, LOFTER, Bilibili, Weibo), the admin web settings page and gallery-dl config must scale. Each new provider adds a tab in the gallery-dl config UI and fields in the API. This is manageable but adds documentation and maintenance burden.

---

## Plan Changes Summary

| Change | Triggered by risk | Mitigation artifact |
|---|---|---|
| RQ selected as queue backend | #B (was #5) | CLAUDE.md -- Core Stack |
| `creator_link.confidence` field added | #3 (was #2) | CLAUDE.md -- Risk-Derived Decisions |
| Job state machine for download_job | #4 | CLAUDE.md -- Risk-Derived Decisions |
| Worker startup orphan reconciliation | #4 | subscription_sync.py -- stale detection |
| `last_successful_auth` on subscription_source | #C (was #7) | subscription_source model |
| `auth_healthy` on subscription_source | #C (was #7) | subscription_source model |
| Admin-triggered Meilisearch re-indexing for v1 | #5 (was #6) | CLAUDE.md -- Risk-Derived Decisions |
| Integration smoke test for gallery-dl output format | #2 (was #1) | Test suite |
| X provider explicitly disabled (placeholder) | #A (was #3) | x.py provider -- can_download=False |
| pyvips preferred over Pillow | #11 | CLAUDE.md -- Risk-Derived Decisions |
| JWT auth with access+refresh token | #E (was #14) | auth.py, auth_api.py, auth.tsx |
| Iwara fully enabled (was placeholder) | #D (was #13) | iwara.py provider -- can_download=True |
| Connectivity test per source | #C (was #7) | admin.py POST /gallerydl-config/test-connection |
| Auth status page in admin-web | #C (was #7) | Settings -> Auth Status |
| Scheduler tolerance window + restart dedup | #F (new) | subscription_sync.py, seed_sync.py |
| Timezone-aware scheduling | #G (new) | subscription_sync.py, ZoneInfo, TIMEZONE config |
| Dockerfile removes SearchTimeline fallback | #H (new) | Dockerfile |
