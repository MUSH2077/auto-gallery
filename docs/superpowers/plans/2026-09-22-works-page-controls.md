# Works Page Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the crowded works toolbar with balanced filter, sort, and display panels; add masonry, persistent card controls, stable random browsing, and source-aware heat sorting.

**Architecture:** Search/filter/sort state remains canonical in `q`, with a separate validated random `seed`; display state extends the existing cross-device appearance preference. Backend work/source projections materialize indexed heat and shuffle fields, while a scheduled Pixiv ranking snapshot augments a deterministic local fallback. All three layouts consume one card behavior model.

**Tech Stack:** FastAPI, SQLAlchemy 2, PostgreSQL 16, Meilisearch, RQ, Next.js 16 App Router, React 19, TanStack Query, Tailwind CSS 4, Playwright.

**Spec:** User-approved specification in this task, reproduced by the Summary and Global Constraints below.

## Global Constraints

- Preserve all existing search syntax, permission-based SFW enforcement, cursor integrity, curation actions, and 30-item page bounds.
- Preserve the user's uncommitted root `.env.example` change; work only on `codex/works-page-controls`.
- Do not use `ORDER BY random()` or add a masonry dependency.
- UI defaults: grid, medium cards, selection/AI/NSFW/favorite visible, existing preview preference preserved, NSFW blur enabled.
- Pixiv official daily ranking is authoritative when fresh; local heat is explicitly a source/age-cohort fallback, not a recreation of Pixiv's private formula.
- Detail-page media behavior is out of scope.

## Review Focus

- Random cursor wrap must neither duplicate nor omit filtered works, and a cursor from another seed must be rejected.
- Missing/stale ranking or engagement metadata must degrade to stable null-last ordering without blocking imports or search.
- A legacy `view` URL may temporarily override preferences, but an explicit layout change must remove that override.
- Hiding selection with selected works must clear invisible state while retaining trash-row actions.
- NSFW permission filtering remains server-side even if badges or blur are disabled.

---

### Task 1: Heat and Ranking Data Foundation

**Files:**
- Create: `backend/app/services/work_heat.py`, `backend/app/models/source_ranking_snapshot.py`, one Alembic migration
- Modify: `backend/app/models/work.py`, `backend/app/models/work_source.py`, `backend/app/models/__init__.py`, Pixiv remote adapter/job integration
- Test: `backend/tests/test_work_heat.py`, `backend/tests/test_pixiv_ranking_sync.py`

**Interfaces:**
- Produces `extract_source_metrics(source, metadata, observed_at)`, `fallback_heat_rows(...)`, and `recompute_source_heat(db, sources)`.
- Produces `SourceRankingSnapshot` and materialized `Work.heat_score`, `Work.heat_observed_at`, `Work.shuffle_key`.
- Ranking sync consumes the existing healthy-account adapter and scheduled queue; later search projection consumes materialized fields.

- [ ] Write tests proving metric extraction, four age cohorts, minimum cohort widening, Bayesian tie breaking, fresh/stale official rank precedence, multi-source maximum, stable UUID shuffle keys, and graceful missing metrics.
- [ ] Run the focused tests and confirm they fail because the heat service/models do not exist.
- [ ] Add nullable model fields, ranking snapshots, deterministic backfill/index migration, and provider metric extraction.
- [ ] Implement pure heat calculation first, then transactional source recomputation and changed-work projection requests.
- [ ] Extend the Pixiv adapter with bounded daily ranking pages and add a scheduled sync job for `day`, `day_ai`, `day_r18`, and `day_r18_ai`, including 30/120-minute retry and 48-hour staleness behavior.
- [ ] Run focused tests, migration checks, and backend unit suite; commit the task.

### Task 2: Search, Cursor, and List API

**Files:**
- Modify: `backend/app/services/search_language.py`, `backend/app/services/search.py`, `backend/app/api/search.py`, `backend/app/schemas/work.py`
- Test: `backend/tests/test_search_language.py`, `backend/tests/test_search.py`, focused cursor/search delivery tests

**Interfaces:**
- Adds `sort:heat-desc` and `sort:random`.
- Adds `seed: int | None` to `/api/v1/search`; random cursor payload binds seed, ring phase, and boundary.
- Adds nullable `thumbnail_width` and `thumbnail_height` to work list/search documents.

- [ ] Write failing parser/API/cursor tests for heat, random, invalid seeds, seed mismatch, ring wrap, null-last heat, deterministic ties, and thumbnail dimensions.
- [ ] Add indexed PostgreSQL heat/random ordering and matching Meilisearch sortable fields/range queries without full-table random sort.
- [ ] Ensure explicit sorts outrank textual relevance; retain relevance only for `sort:relevance`.
- [ ] Include `seed` in frontend-compatible response flow and search query identity; preserve old clients with an optional parameter.
- [ ] Run focused, OpenAPI, search-delivery, and backend unit suites; commit the task.

### Task 3: Appearance Preferences and Reusable Works Controls

**Files:**
- Modify: `admin-web/src/lib/appearance.tsx`
- Create: focused works control/card/layout components under `admin-web/src/app/admin/works/`
- Test: `admin-web/tests/e2e/works-controls.spec.ts`

**Interfaces:**
- Extends `AppearanceSettings` with layout, card size, four visibility flags, and NSFW blur.
- Produces one responsive control surface: anchored desktop popover and modal mobile sheet.
- Produces a shared card presentation contract used by grid, list, and masonry.

- [ ] Add failing Playwright scenarios for defaults, persisted preference hydration, one-panel behavior, Escape/outside close, focus return, mobile sheet semantics, and display toggles.
- [ ] Extend version-tolerant appearance sanitization and cross-device persistence.
- [ ] Implement Filter, Sort, and Display panels with accessible labels, staged filter apply/cancel, immediate sort/display updates, and advanced preview link.
- [ ] Implement shared card visibility/blur behavior and size tokens without fetching data on appearance changes.
- [ ] Run focused E2E, typecheck, bundle/search/i18n checks; commit the task.

### Task 4: Works Page Integration and Three Layouts

**Files:**
- Modify: `admin-web/src/app/admin/works/page.tsx`, API client/types, bilingual catalog
- Test: `admin-web/tests/e2e/works-controls.spec.ts` and existing works/media/mobile specs

**Interfaces:**
- `q` remains canonical filter/sort state; random uses URL `seed`; legacy `view` is override-only.
- Repeated positive `source:` and `has:` qualifiers remain OR groups.
- Search clear removes `q`, `p`, and `seed`; display preferences remain.

- [ ] Add failing integration scenarios for controlled qualifier replacement, summaries/counts, sort options, reshuffle, search clear, legacy view precedence, selection clearing, trash actions, and permissions.
- [ ] Replace the old toolbar and both clear buttons with the balanced control bar.
- [ ] Wire stable query keys/prefetch to `q/page/seed`; remove seed when leaving random and generate a new seed only on explicit reshuffle.
- [ ] Render grid/list/masonry from the shared card contract; reserve masonry aspect ratio from API dimensions and retain content visibility.
- [ ] Add bilingual copy and regenerate checked catalogs/API types.
- [ ] Run focused E2E, all relevant frontend checks, and production build; commit the task.

### Task 5: Scale, Rollout, and Whole-Feature Verification

**Files:**
- Modify: benchmark/acceptance coverage and rollout documentation only where required by executable behavior
- Test: backend performance/search suites and frontend Playwright regression set

**Interfaces:**
- Release gate: 70,000 works, 30-item pages, heat/random p95 below 500 ms, existing sort p95 regression at most 10%.
- Rollout order is schema/backend, shuffle/heat backfill and Meilisearch rebuild, then frontend activation.

- [ ] Add a release-shaped benchmark for first and cursor pages of heat/random and a query-plan assertion forbidding full-table random sort.
- [ ] Verify migration upgrade, idempotent backfill/recompute, search rebuild, and forward-schema application rollback.
- [ ] Run backend suite, frontend type/lint-contract/build checks, and desktop/mobile Playwright scenarios; record exact outcomes.
- [ ] Request whole-branch code review, fix Critical/Important findings with RED→GREEN tests, and rerun full verification.
