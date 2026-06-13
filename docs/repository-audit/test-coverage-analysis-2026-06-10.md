# Test Coverage Analysis — Structural Risk Assessment

**Date**: 2026-06-10
**Overall line coverage**: 35% (4,554 / 7,005 lines missed)
**Test count**: 150 test functions in 14 files
**Suite runtime**: ~3 seconds

---

## Executive Summary

The test suite has **dangerous blind spots**, not low coverage percentages. The three most critical workflows — download, import, and batch import — have **0% behavioral test coverage**. The repository layer (all data access) has **13–29% coverage**. Meanwhile, 25 of 150 tests (~17%) are **source-code inspection tests** that verify string literals exist in source code rather than verifying behavior — providing false confidence with no real protection.

**Risk rating: HIGH** — The system cannot reliably detect regressions in data integrity, import correctness, or download behavior.

---

## 1. Critical Workflow Coverage Gaps

### A. Download Pipeline — 0% coverage (CRITICAL)

| File | Lines | Coverage | Risk |
|------|-------|----------|------|
| `jobs/download.py` | 351 | **0%** | Data loss |
| `jobs/batch_import.py` | 227 | **0%** | Mass import failure |

`run_download_job()` orchestrates gallery-dl subprocess invocation with config, auth health monitoring from stderr, file snapshot before/after diff, import job enqueuing, retry with exponential backoff, and partial recovery on timeout/failure. **Zero tests exist for any of these behaviors.**

### B. Import Pipeline — 20% coverage (helpers only) (CRITICAL)

| File | Lines | Coverage | Risk |
|------|-------|----------|------|
| `jobs/import_runner.py` | 371 | **20%** | Data corruption |

The 20% comes exclusively from `test_import_detection.py` testing module-level helper functions. The core `run_import_job()` function — which creates Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag records — has **zero behavioral tests**.

**Untested import behaviors:** SourceCreator upsert, Work deduplication idempotency, Asset-to-WorkSource linking, Tag normalization and deduplication, thumbnail generation error handling, pHash/sha256 computation error handling, metadata.json writing, Meilisearch batch indexing during import, JSON cleanup, empty directory cleanup, partial import recovery.

### C. Repository Layer — 13–29% coverage (HIGH)

| File | Lines | Coverage |
|------|-------|----------|
| `repositories/work.py` | 93 | **13%** |
| `repositories/creator.py` | 102 | **19%** |
| `repositories/subscription.py` | 71 | **24%** |
| `repositories/download_job.py` | 65 | **29%** |
| `repositories/tag.py` | 42 | **29%** |

The entire data access layer is effectively untested. `CreatorRepository.list_all()` has 4 subqueries with complex JOINs, a Meilisearch fallback path, and 6 optional filter conditions. **Not one behavioral test exists.**

### D. Service Layer — 12–29% coverage (HIGH)

| File | Lines | Coverage |
|------|-------|----------|
| `services/creator.py` | 113 | **19%** |
| `services/subscription.py` | 119 | **18%** |
| `services/download.py` | 151 | **28%** |
| `services/search.py` | 143 | **15%** |
| `services/subscription_enqueue.py` | 109 | **21%** |
| `services/cache.py` | 55 | **29%** |
| `services/thumbnail.py` | 19 | **0%** |

---

## 2. Test Quality Issues

### 2.1 Source-Code Inspection Tests (CRITICAL — false confidence)

**25 of 150 tests (~17%) use `inspect.getsource()` to check for string literals in source code rather than calling functions and asserting behavior.**

Examples from `test_creator_dedup.py`:
```python
def test_cannot_merge_into_self(self):
    src = inspect.getsource(merge_creators)
    assert "Cannot merge a creator into itself" in src  # Tests a STRING

def test_merge_returns_stats(self):
    src = inspect.getsource(merge_creators)
    assert '"links_moved"' in src  # Tests a dict key literal
```

Examples from `test_search.py`:
```python
def test_module_imports(self):
    assert MeiliClient is not None  # Tests import succeeds

def test_search_method_signature(self):
    sig = inspect.signature(SearchService.search)
    assert "query" in params  # Tests parameter names
```

**Why dangerous:** Renaming a variable → false negative. Removing logic but keeping the string → false positive. Provides zero behavioral verification while counting toward line coverage.

### 2.2 Missing Negative Tests

No tests verify: gallery-dl returning corrupt JSON, Redis unavailable, Meilisearch unavailable, disk full during download, invalid creator URL, duplicate source_creator_id, missing asset files, concurrent import race conditions.

### 2.3 Missing Contract Tests

No tests validate API response shapes against Pydantic schemas. No OpenAPI schema validation.

### 2.4 Missing Integration Boundaries

All 150 tests are unit tests. No test uses a real PostgreSQL database, exercises HTTP through FastAPI TestClient, runs gallery-dl as a subprocess, or reads/writes actual files on disk.

### 2.5 Flaky Tests

**None detected.** Suite runs in ~3 seconds with no retry markers.

### 2.6 Tests Without Meaningful Assertions

```python
def test_get_recent_returns_list(self):
    entries = get_recent(10)
    assert isinstance(entries, list)  # Could be empty — no behavioral assertion

def test_function_exists(self):
    assert inspect.iscoroutinefunction(find_merge_candidates)  # Tests type, not behavior
```

---

## 3. What IS Well-Tested

| Area | Quality | Notes |
|------|---------|-------|
| Provider parsing | **Good** | 54 tests covering URL validation, metadata parsing, capabilities |
| AI/NSFW detection | **Good** | 21 tests with per-source edge cases |
| Danbooru URL classification | **Good** | 18 tests covering all URL types |
| Auth password rotation | **Good** | 7 tests covering token lifecycle |
| Scheduler timing logic | **Good** | 11 tests with timezone/window edge cases |
| Settings config merging | **Good** | 7 tests with override priority |
| Job state machine | **Adequate** | 5 tests covering valid/invalid transitions |
| Proxy env configuration | **Adequate** | 2 tests for enabled/disabled |

---

## 4. Risk-Based Coverage Plan

### Priority 1 — Data Integrity (implement first)

**1A. Import pipeline behavioral tests** (`tests/test_import_pipeline.py`)

Requires a test database fixture with real PostgreSQL:
- `test_import_creates_work_from_metadata()`
- `test_import_upserts_source_creator()`
- `test_import_skips_existing_work_source()` (idempotency)
- `test_import_creates_assets_with_dimensions()`
- `test_import_normalizes_tags_deduplicating_by_name()`
- `test_import_creates_work_source_tags_with_original_name()`
- `test_import_writes_metadata_json_to_library()`
- `test_import_deletes_processed_json_files()`
- `test_import_handles_corrupt_metadata_json()`
- `test_import_sets_ai_flag_from_pixiv_metadata()`
- `test_import_sets_nsfw_flag_from_pixiv_metadata()`
- `test_import_computes_sha256_for_asset()`

**Risk reduction**: Catches data corruption, duplicate records, missing relationships, tag loss.

**1B. Repository query behavioral tests** (`tests/test_repositories.py`)

- `test_list_creators_returns_paginated_results()`
- `test_list_creators_filters_by_is_active()`
- `test_list_creators_filters_by_search()`
- `test_list_creators_includes_subscription_count()`
- `test_list_works_filters_by_source_and_creator()`
- `test_get_creator_timeline_returns_daily_counts()`

**Risk reduction**: Catches SQL bugs, pagination errors, incorrect aggregations.

### Priority 2 — User Workflows (implement second)

**2A. API integration tests** (`tests/test_api_contracts.py`)

Using FastAPI TestClient:
- Creator CRUD with response shape validation
- Subscription create/sync-now flow
- Batch operations returning per-item results
- Auth-required endpoint returns 401

**2B. Download pipeline unit tests** (`tests/test_download_pipeline.py`)

- Command construction with config/archive/range
- Auth error detection from stderr
- Retry-on-failure with backoff
- Partial recovery on timeout
- Skip import when no new metadata

### Priority 3 — Failure Paths (implement third)

**3A. Negative scenario tests**

- Redis unavailable → cache returns None, lock times out
- Meilisearch unavailable → search returns empty, indexing logs warning
- Corrupt metadata JSON → import skips work, logs error
- Duplicate source_creator_id → upsert updates instead of inserting

### Priority 4 — Replace Inspection Tests (alongside above)

Replace 25 `inspect.getsource()` tests in `test_creator_dedup.py` and `test_search.py` with behavioral tests that call functions and assert results against a real database.

---

## 5. What NOT to Test

- **Pydantic schemas** (100%): FastAPI validates these at runtime. Testing schema fields tests Pydantic, not our code.
- **SQLAlchemy model definitions** (100%): Tested implicitly by any test using the database.
- **Provider `parse_assets()` methods**: Currently unused by import runner. Wire it in or remove it before testing.
- **Arbitrary percentage targets**: 80% global coverage would require testing trivial code (config loading, module init).

---

## 6. Test Infrastructure Recommendations

1. **Test database fixture**: Real PostgreSQL (Docker) with Alembic migrations applied
2. **Factory fixtures**: `create_test_creator()`, `create_test_subscription()` helpers
3. **`pytest.mark.slow`**: Mark integration tests. Unit tests in pre-commit, full suite in merge CI.
4. **Contract test helper**: Validate API responses against Pydantic `response_model` schemas.
5. **Coverage threshold**: Set `--cov-fail-under=35` now, raise to 50% after Priority 1 implemented.

---

## Summary

| Priority | Area | Current | Target | New Tests |
|----------|------|---------|--------|-----------|
| **1A** | Import pipeline | 0% behavioral | 70%+ | ~12 |
| **1B** | Repository queries | 13–29% | 60%+ | ~6 |
| **2A** | API contracts | 13–52% | 50%+ | ~8 |
| **2B** | Download pipeline | 0% behavioral | 60%+ | ~7 |
| **3A** | Negative scenarios | 0% | Presence | ~6 |
| **4A** | Replace inspection tests | N/A | Behavioral | Replace 25 |

**Total new behavioral tests: ~39 | Inspection tests to replace: 25 | Estimated effort: 3–5 days**
