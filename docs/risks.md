# Risk Register

## HIGH Severity

### 0. Multi-user data model transition

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

### 1. gallery-dl version / output format stability

**Risk**: gallery-dl updates frequently. Pinning a version means site-specific extractors may break when source sites change. Not pinning means output format changes can silently break metadata parsers.

**Impact**: Broken downloads, failed imports, missing metadata

**Mitigation**:
- Pin a known-good gallery-dl version in `requirements.txt`
- Add integration smoke tests: run gallery-dl against a known URL, verify JSON output structure matches parser expectations
- When upgrading gallery-dl, run the smoke test suite first
- Version the gallery-dl config schema

**Decision**: Pin version + smoke tests. Do not auto-upgrade.

---

### 2. Cross-source creator identity mapping

**Risk**: Linking accounts across platforms ("Pixiv user 123456" ↔ "@artist_handle on X") is fundamentally hard. Profile URLs are embedded in free-text fields. Many creators use completely different names. Automation will have limited accuracy.

**Impact**: Manual mapping burden on admin. Fragmented creator records.

**Mitigation**:
- Accept that automatic mapping will be ~60% accurate at best
- Start with manual mapping as the primary path
- Danbooru reference enrichment is a suggestion engine, not an authority
- Add `creator_link.confidence` field (0.0–1.0) for suggested links
- Admin review queue for suggested links
- Do NOT make automatic mapping a dependency for Phases 1-6

**Decision**: Manual-first with suggestion engine. Architecture handles this.

---

### 3. X/Twitter API viability

**Risk**: X has aggressively restricted API access. gallery-dl's X extractor may break permanently or require paid API access.

**Impact**: X/Twitter may not be a viable source for automated downloading.

**Mitigation**:
- Keep X as a placeholder provider with `can_download = False`
- Focus Phase 1-8 on Pixiv + local import + manual upload
- If gallery-dl X support is viable later, activate it
- If not, X import may require a different approach (browser extension, RSS, manual)
- The provider abstraction already handles this — no architecture change needed

**Decision**: X is explicitly "no timeline." Re-evaluate after Pixiv pipeline is stable.

---

### 4. DOWNLOAD_ROOT → LIBRARY_ROOT atomicity

**Risk**: gallery-dl writes to `DOWNLOAD_ROOT/<job_id>/`. Import moves files to `LIBRARY_ROOT/`. If the worker crashes between these steps:
- Files in DOWNLOAD_ROOT with no import job (orphaned downloads)
- DB records pointing to files that failed to move (broken references)

**Impact**: Storage waste, broken media URLs, inconsistent database

**Mitigation**:
- Job state machine: `pending → downloading → downloaded → importing → complete`
- On worker startup, scan DOWNLOAD_ROOT for orphaned directories, reconcile with `download_job` table
- Import must be fully idempotent: re-running with same files creates no duplicates
- Use `(source, source_asset_id)` uniqueness to prevent duplicate asset records
- File moves within the same filesystem (same Docker volume) are atomic if using `os.rename`

**Decision**: State machine + startup reconciliation + idempotent imports.

---

## MEDIUM Severity

### 5. Queue backend: RQ chosen

**Risk**: RQ vs Celery was undecided. RQ is simpler but has fewer failure-recovery features.

**Decision made**: RQ for v1. Rationale: uses Redis (already in stack), simpler config, job model already abstracts the queue. Migrating RQ → Celery is feasible because `download_job`/`import_job` tables are the source of truth.

---

### 6. Meilisearch index consistency

**Risk**: Dual-writes to PostgreSQL and Meilisearch can drift. Periodic re-indexing means stale search results.

**Mitigation**:
- v1: Admin-triggered full re-indexing (manual, but consistent)
- v2: Application-level dual-writes with periodic reconciliation
- Accept that search results may be up to N minutes stale in v1

---

### 7. gallery-dl auth/cookie expiration

**Risk**: Pixiv session cookies expire. Failed auth results in silent download failures.

**Mitigation**:
- Add `last_successful_auth` timestamp to `subscription_source`
- Health endpoint detects sources with recent auth failures
- Admin cookie/config status page shows auth health per source
- Consider: can we detect HTTP 401/403 in gallery-dl output?

---

### 8. Provider abstraction leak

**Risk**: Pixiv metadata (series, caption structure) differs significantly from Iwara (voice actors, 3D tags) and X (hashtags, thread context). Forcing all through identical `parse_*` methods may produce awkward lowest-common-denominator schemas.

**Mitigation**:
- `raw_metadata` JSONB column is the escape hatch — everything source-specific lives there
- Typed fields capture only what maps cleanly
- Admin web may have provider-specific components that render `raw_metadata` per source
- Provider `parse_*` methods extract best-effort typed data; nothing is discarded

---

### 9. Single worker bottleneck

**Risk**: One worker processes one job at a time. A creator with 1000+ works takes hours/days to fully sync.

**Mitigation**:
- Accept for v1 (personal archive, not real-time service)
- Worker can be scaled: `docker compose up --scale worker=3`
- Scaling works as long as each job uses a unique DOWNLOAD_ROOT subdirectory (`<job_id>`)
- Scheduler can stagger subscription syncs to avoid thundering herd

---

### 10. Multi-user storage sharing

**Risk**: Multiple users subscribing to the same creator would download the same files repeatedly if data isn't shared. Creator/Work/Asset must be global, not per-user. But this creates a privacy consideration: one user's subscription reveals the creator to all users.

**Mitigation**:
- `creator`, `work`, `asset`, `tag` tables are global (shared, no user_id)
- `subscription`, `album`, `download_job` tables are user-scoped (have user_id)
- Download once, reference many times via `asset_sources`
- Creator list is public to all authenticated users (acceptable for a personal/small-group archive)
- If private creators are needed later: add `creator.visibility` field

**Decision**: Shared creator/work/asset data model. User-scoped subscriptions and albums only.

---

### 11. Remote client bandwidth and image loading

**Risk**: Flutter client loading full-resolution images over mobile data will be slow and consume excessive bandwidth.

**Mitigation**:
- Generate thumbnails at import time: 200px, 600px, 1200px widths
- Client API returns thumbnail URLs alongside full-resolution URLs
- Client fetches thumbnails for grid/list views, full image only on detail view
- Add `?width=` query param to `/media/` for dynamic resizing (future, pyvips can do this)
- Cache-Control headers on media: long max-age for immutable assets

**Decision**: Multi-size thumbnails generated at import. No on-the-fly resizing in v1.

---

### 12. Client auth token lifecycle

**Risk**: JWT access tokens expire every 15 minutes. Flutter client must transparently refresh. If refresh logic is buggy, user sees auth errors. If refresh token expires (30 days), user must re-login. Remote re-login when not on LAN is impossible without VPN.

**Mitigation**:
- Flutter HTTP interceptor (dio) handles 401 → refresh → retry transparently
- Refresh token stored in platform secure storage
- On refresh failure: clear tokens, redirect to login screen
- Refresh token expiry: show notification 7 days before expiry
- Admin can issue long-lived access tokens for trusted clients (future)

**Decision**: Standard JWT access+refresh pattern. dio interceptor for transparent refresh.

---

### 13. NAS HDD I/O performance
- Generate thumbnails at import time, not on first read request
- Accept that initial sync of a large creator will be slow

---

## LOW Severity

### 11. Alembic migration execution
Run via `docker compose run --rm backend alembic upgrade head` before starting the stack. Add a startup check that refuses to start if migrations are pending.

### 12. File naming / reorganization
gallery-dl names files during download. Import job can reorganize. Asset paths are stored in DB after import, not before. No risk of broken paths if naming_template changes between download and import.

### 13. Iwara gallery-dl support unknown
Verify gallery-dl supports Iwara before Phase 8. If not, Iwara remains a placeholder. No architecture impact.

### 14. Admin auth mechanism
Decide before Phase 6: simple API key for v1 admin routes. JWT + multi-user later. This does not affect the model layer.

### 15. Danbooru phase ordering
Creator identity (Phase 4) works without Danbooru (Phase 5). Danbooru enriches identity, doesn't enable it. Correctly ordered.

---

## Plan Changes Summary

| Change | Triggered by risk | Mitigation artifact |
|---|---|---|
| RQ selected as queue backend | #5 | [CLAUDE.md](../.claude/CLAUDE.md) — Core Stack |
| `creator_link.confidence` field added | #2 | [backend-architecture.md](../.claude/skills/backend-architecture.md) — Risk-derived model fields |
| Job state machine for download_job | #4 | [gallerydl-integration.md](../.claude/skills/gallerydl-integration.md) — Download job state machine |
| Worker startup orphan reconciliation | #4 | [gallerydl-integration.md](../.claude/skills/gallerydl-integration.md) — Worker startup orphan reconciliation |
| `last_successful_auth` on subscription_source | #7 | [backend-architecture.md](../.claude/skills/backend-architecture.md) — Risk-derived model fields |
| `auth_healthy` on subscription_source | #7 | [gallerydl-integration.md](../.claude/skills/gallerydl-integration.md) — Auth health tracking |
| Admin-triggered Meilisearch re-indexing for v1 | #6 | [backend-architecture.md](../.claude/skills/backend-architecture.md) — Meilisearch sync |
| Integration smoke test for gallery-dl output format | #1 | [backend-architecture.md](../.claude/skills/backend-architecture.md) — gallery-dl smoke test |
| X provider explicitly "no timeline" | #3 | [CLAUDE.md](../.claude/CLAUDE.md) — Risk-Derived Decisions |
| pyvips preferred over Pillow | #10 | [CLAUDE.md](../.claude/CLAUDE.md) — Risk-Derived Decisions |
