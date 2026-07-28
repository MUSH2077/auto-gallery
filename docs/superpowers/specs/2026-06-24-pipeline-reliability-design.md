# Pipeline Reliability — Correctness + Observability (Design)

**Date:** 2026-06-24
**Branch:** gitlike-gallery
**Status:** Approved (design) → pending implementation plan
**Scope:** Backend job pipeline (`run_download_job`, `run_import_job`). No schema migration, no state-machine change, no API/UI change.

---

## 1. Problem

After recent firefighting the pipeline already has a skip-rate failure path, an
`ArtifactLedger`, heartbeat, dual stall/overall timeouts, and batch indexing.
Five concrete gaps remain — all observed in production sessions.

| # | Gap | Evidence | Impact |
|---|-----|----------|--------|
| G1 | **Empty-result silent success** | `import_runner.py:723` guard `total_works > 0` ⇒ 0 parsed groups marked `complete` with 0 works | "导入完成但看不到作品" — completed-but-empty |
| G2 | **Cannot distinguish "nothing parsed" from "all already imported"** | idempotency skip (`import_runner.py:415-427`) increments no counter | Outcome cannot be classified honestly |
| G3 | **Skip threshold hardcoded `0.5`** | `import_runner.py:723` | Not tunable; magic number (audit P1-3) |
| G4 | **Swallowed exceptions** | `download.py:339`, `download.py:470` | Real config/runtime errors hidden (audit P1-5) |
| G5 | **No per-stage timing** | manifest records events but no durations | Cannot localize where stalls/slowness occur |

## 2. Goals / Non-goals

**Goals**
- An import or download can never end `complete` while having produced nothing
  real. Empty/failed outcomes are loud, with a reason on the job record.
- Distinguish *failure* (nothing parsed / high skip) from *legitimate no-op*
  (everything already imported).
- Make the skip threshold configurable.
- Eliminate silent `except Exception: pass` in the download path.
- Per-stage timing visible in the existing manifest.

**Non-goals (separate specs)**
- Decomposing the 975-line / 846-line job functions (beyond the one helper
  extracted for testability below).
- A new `partial` status, DB migration, or state-machine change.
- Frontend/UI work; new metrics infrastructure (Prometheus/Grafana).
- Surfacing stays in `error_log` + manifest events + structured logs, which the
  existing job-detail view and log API already render.

## 3. Design

### 3a. Import outcome classification (G1, G2)
Extract the decision into a pure, unit-testable helper:

```python
def classify_import_outcome(
    *, total_groups: int, works: int, assets: int,
    existing: int, skipped: int, threshold: float,
) -> tuple[str, str]:
    """Return (status, human_message). status ∈ {"complete", "failed"}."""
```

**Approved rules (in order):**
1. `total_groups == 0` → **`failed`** — "gallery-dl produced no parseable
   metadata groups (0 found)".
2. `skipped > 0 and works == 0` → **`failed`** — every parsed work failed.
3. `total_groups > 0 and skipped > 0 and skipped / total_groups >= threshold`
   → **`failed`** — high skip rate (provider/gallery-dl format drift).
4. otherwise → **`complete`** — includes the all-`existing` re-run (nothing new
   to import is a legitimate success).

`run_import_job` adds an `existing` counter to `stats`, incremented on the
idempotency-skip path. `error_log` always receives a one-line summary:
`works=N assets=M existing=E skipped=S; top reasons: <reason×count, …>`.
Both `import_job` and parent `download_job` are transitioned with the same
classification (preserving the current dual-update behavior).

### 3b. Download empty-artifact loudness (G1, approved decision #2)
When the post-download artifact scan finds **0 new metadata AND 0 new media**,
mark the `download_job` **`failed`** with reason
"gallery-dl exited 0 but produced no artifacts" instead of proceeding to a
no-op import. Matches the CLAUDE.md gotcha "exit code 0 does NOT guarantee
files". Retry/backoff already in place still applies.

### 3c. Configurable threshold (G3)
Read `import_skip_threshold` (default `0.5`, clamped to `[0, 1]`) from
`get_download_defaults()` (`settings.py:142`), mirroring `stall_timeout_seconds`.
Passed into `classify_import_outcome(threshold=…)`.

### 3d. Swallowed-exception cleanup (G4)
`download.py:339` and `download.py:470`: replace bare `except Exception: pass`/
`logger.debug` with `logger.warning(..., exc_info=True)`, narrowing the caught
type where the failure mode is known (e.g. Redis health check). No behavior
change beyond visibility.

### 3e. Stage timing (G5)
A small context-manager helper times each phase
(`download`, `scan`, `parse`, `thumbnail_hash`, `index`) and emits
`append_manifest_event(job, "stage_timing", stage=<name>, ms=<int>)`. Reuses the
manifest already shown in the job detail. No new storage.

## 4. Testing (TDD-first)

New table-driven tests; classification is pure so it is fully unit-testable.

`backend/tests/test_import_outcome.py` — `classify_import_outcome`:
- 0 groups → `failed`
- all-existing (groups>0, works=0, skipped=0, existing=all) → `complete`
- 100% no-media (works=0, skipped=all) → `failed`
- skip rate exactly at threshold → `failed`; just below → `complete`
- healthy (works>0, skipped=0) → `complete`
- `error_log` summary contains the counts

`backend/tests/test_download_artifacts.py`:
- 0 metadata + 0 media scan → download `failed` with the no-artifacts reason
- ≥1 artifact → proceeds to import enqueue

Write the failing tests first, then implement until green
(`docker compose exec backend python -m pytest`).

## 5. Risk & rollout

- **Low blast radius:** no schema/state-machine/API/UI changes; reuses existing
  `complete`/`failed` statuses, `error_log`, `stats`, and manifest JSON.
- **Behavior-preserving except the closed gaps:** with default threshold `0.5`,
  the only new failures are previously-silent empty/all-skipped outcomes — which
  is the intended correction.
- Each sub-change (3a–3e) is independently revertable.
- Verified by the standard Completion Workflow: `pytest` → `scripts/deploy.sh`
  → commit; confirm via `docker compose logs worker-import`.

## 6. Resolved decisions

1. Classification: 0 parsed groups **or** works==0-with-skips ⇒ `failed`
   (not a softer "degraded but complete"). — **confirmed**
2. Download 0-artifact ⇒ `failed` (not left `downloaded`). — **confirmed**
