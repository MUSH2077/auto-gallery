# Risk Register

## HIGH Severity

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

### 10. NAS HDD I/O performance

**Risk**: Computing SHA-256 and pHash on large media files over NAS HDD (possibly SMR drives) will be slow.

**Mitigation**:
- Compute hashes during download streaming where possible
- Use `pyvips` over Pillow for large images (faster, less memory)
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
