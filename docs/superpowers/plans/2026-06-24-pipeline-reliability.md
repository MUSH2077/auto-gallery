# Pipeline Reliability (Correctness + Observability) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the download/import pipeline incapable of silently finishing `complete` while producing nothing real, surface skip/empty reasons + per-stage timing, and make the skip threshold configurable — without schema, state-machine, or API changes.

**Architecture:** Extract the two outcome *decisions* into pure, unit-tested helper modules (`import_outcome.py`, `download_outcome.py`), then wire them into the existing giant job functions. Add an `existing` counter to distinguish "nothing parsed" (failure) from "all already imported" (legitimate no-op). Record stage durations as manifest events.

**Tech Stack:** Python 3.12, async SQLAlchemy 2.0, RQ, pytest (run inside the `backend` container).

## Global Constraints

- Run tests with: `docker compose exec backend python -m pytest <path> -v` (tests need the container; do NOT run host pytest).
- No DB migration, no new status enum value, no state-machine change. Reuse existing `complete`/`failed` statuses, `error_log`, `stats`, and the manifest JSON.
- `shell=True` FORBIDDEN; never log DB credentials; source-agnostic naming (constraints in `.claude/constraints/security.md`, `source-abstraction.md`).
- Commit message trailer, exactly: `Co-Authored-By: Claude <noreply@anthropic.com>`.
- Line numbers below are anchors from 2026-06-24; if drifted, locate by the quoted surrounding code, not the number.

## Design refinement to spec decision #2 (READ FIRST)

The spec said "download 0-artifact ⇒ failed". Grounding revealed [download.py:895-916](../../../backend/app/jobs/download.py#L895) already marks 0-metadata+0-media as `complete`. Flipping ALL such jobs to `failed` would regress **subscription re-syncs**, which legitimately produce 0 artifacts when there are no new posts (gallery-dl archive skip). **Refinement:** 0-artifact ⇒ `failed` only for **manual** downloads (`subscription_source_id is None`); subscription re-syncs with no new content stay `complete`. Auth-warning empty results remain `failed` (unchanged). This is implemented in Task 4/5.

---

### Task 1: `classify_import_outcome` + threshold clamp (pure helper, TDD)

**Files:**
- Create: `backend/app/jobs/import_outcome.py`
- Test: `backend/tests/test_import_outcome.py`

**Interfaces:**
- Produces: `classify_import_outcome(*, total_groups: int, works: int, assets: int, existing: int, skipped: int, threshold: float) -> tuple[str, str]` (status ∈ {"complete","failed"}, message); `clamp_threshold(value: float) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_import_outcome.py
from app.jobs.import_outcome import classify_import_outcome, clamp_threshold


def test_zero_groups_is_failed():
    status, msg = classify_import_outcome(
        total_groups=0, works=0, assets=0, existing=0, skipped=0, threshold=0.5)
    assert status == "failed"
    assert "0" in msg


def test_all_existing_is_complete():
    status, _ = classify_import_outcome(
        total_groups=5, works=0, assets=0, existing=5, skipped=0, threshold=0.5)
    assert status == "complete"


def test_all_skipped_is_failed():
    status, _ = classify_import_outcome(
        total_groups=4, works=0, assets=0, existing=0, skipped=4, threshold=0.5)
    assert status == "failed"


def test_skip_rate_at_threshold_is_failed():
    status, _ = classify_import_outcome(
        total_groups=4, works=2, assets=2, existing=0, skipped=2, threshold=0.5)
    assert status == "failed"


def test_skip_rate_below_threshold_is_complete():
    status, _ = classify_import_outcome(
        total_groups=10, works=9, assets=9, existing=0, skipped=1, threshold=0.5)
    assert status == "complete"


def test_healthy_is_complete_and_summary_has_counts():
    status, msg = classify_import_outcome(
        total_groups=3, works=3, assets=7, existing=0, skipped=0, threshold=0.5)
    assert status == "complete"
    assert "works=3" in msg and "assets=7" in msg


def test_clamp_threshold():
    assert clamp_threshold(-1.0) == 0.0
    assert clamp_threshold(2.0) == 1.0
    assert clamp_threshold(0.5) == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend python -m pytest backend/tests/test_import_outcome.py -v`
Expected: FAIL — `ModuleNotFoundError: app.jobs.import_outcome`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/jobs/import_outcome.py
"""Pure outcome classification for the import job. No I/O — fully unit-testable."""
from __future__ import annotations

DEFAULT_IMPORT_SKIP_THRESHOLD = 0.5


def clamp_threshold(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def classify_import_outcome(
    *,
    total_groups: int,
    works: int,
    assets: int,
    existing: int,
    skipped: int,
    threshold: float,
) -> tuple[str, str]:
    """Classify an import run. Returns (status, message); status in {"complete","failed"}.

    Rules, in order:
      1. total_groups == 0           -> failed (nothing parsed)
      2. skipped > 0 and works == 0  -> failed (every parsed work failed)
      3. skip rate >= threshold      -> failed (provider/gallery-dl format drift)
      4. otherwise                   -> complete (includes all-existing no-op)
    """
    summary = f"works={works} assets={assets} existing={existing} skipped={skipped}"
    if total_groups == 0:
        return "failed", f"No parseable metadata groups found (0). {summary}"
    if skipped > 0 and works == 0:
        return "failed", f"All parsed works failed to import. {summary}"
    if skipped > 0 and (skipped / total_groups) >= threshold:
        pct = round(skipped / total_groups * 100)
        return "failed", (
            f"High import skip rate: {skipped}/{total_groups} ({pct}%); "
            f"may indicate provider/gallery-dl format change. {summary}"
        )
    return "complete", f"Import complete. {summary}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend python -m pytest backend/tests/test_import_outcome.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/import_outcome.py backend/tests/test_import_outcome.py
git commit -m "feat(import): pure classify_import_outcome helper with tests

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Add `import_skip_threshold` to download defaults

**Files:**
- Modify: `backend/app/services/settings.py` (`get_download_defaults`, ~line 142-153)
- Test: `backend/tests/test_download_defaults_threshold.py`

**Interfaces:**
- Consumes: `app.jobs.import_outcome.DEFAULT_IMPORT_SKIP_THRESHOLD`, `clamp_threshold`.
- Produces: `get_download_defaults(db)` result now contains key `"import_skip_threshold": float`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_download_defaults_threshold.py
import pytest
from app.services import settings as settings_mod


@pytest.mark.asyncio
async def test_download_defaults_has_skip_threshold(monkeypatch):
    async def fake_get_system_setting(db, key):
        return {}
    monkeypatch.setattr(settings_mod, "get_system_setting", fake_get_system_setting)
    result = await settings_mod.get_download_defaults(None)
    assert result["import_skip_threshold"] == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend python -m pytest backend/tests/test_download_defaults_threshold.py -v`
Expected: FAIL — `KeyError: 'import_skip_threshold'`.

- [ ] **Step 3: Implement — add the key to `get_download_defaults`**

In `backend/app/services/settings.py`, add the import near the top with the other imports:

```python
from app.jobs.import_outcome import DEFAULT_IMPORT_SKIP_THRESHOLD, clamp_threshold
```

Then in `get_download_defaults` (the returned dict, currently ending with `**defaults,`), add this entry BEFORE `**defaults,`:

```python
        "import_skip_threshold": clamp_threshold(
            float(defaults.get("import_skip_threshold", DEFAULT_IMPORT_SKIP_THRESHOLD))),
```

Resulting tail of the dict:

```python
        "gallerydl_abort": int(defaults.get("gallerydl_abort", DEFAULT_GALLERYDL_ABORT)),
        "import_skip_threshold": clamp_threshold(
            float(defaults.get("import_skip_threshold", DEFAULT_IMPORT_SKIP_THRESHOLD))),
        **defaults,
    }
```

Note: `**defaults` may override with a raw stored value, so consumers MUST re-clamp at read time (done in Task 3).

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend python -m pytest backend/tests/test_download_defaults_threshold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/settings.py backend/tests/test_download_defaults_threshold.py
git commit -m "feat(settings): configurable import_skip_threshold (default 0.5)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Wire outcome classification into `run_import_job`

**Files:**
- Modify: `backend/app/jobs/import_runner.py` (stats init ~317; idempotency skip ~423-427; finalization ~720-779)

**Interfaces:**
- Consumes: `classify_import_outcome`, `clamp_threshold` (Task 1); `get_download_defaults` (Task 2).

- [ ] **Step 1: Add the imports** (top of `import_runner.py`, with the other `app.jobs`/`app.services` imports)

```python
from app.jobs.import_outcome import classify_import_outcome, clamp_threshold
from app.services.settings import get_download_defaults
```

- [ ] **Step 2: Add `existing` to the stats dict**

Find (~line 317):

```python
        stats = {"works": 0, "assets": 0, "multi_page": 0, "skipped": 0}
```

Replace with:

```python
        stats = {"works": 0, "assets": 0, "multi_page": 0, "skipped": 0, "existing": 0}
```

- [ ] **Step 3: Count idempotent skips as `existing`**

Find the idempotency block (~423-427):

```python
                if existing_ws.scalar_one_or_none():
                    async with async_session() as ledger_db:
                        await ArtifactLedger(ledger_db).mark_work(dj.id, src_work_id, "done")
                        await ledger_db.commit()
                    continue
```

Replace with (adds the counter line):

```python
                if existing_ws.scalar_one_or_none():
                    stats["existing"] += 1
                    async with async_session() as ledger_db:
                        await ArtifactLedger(ledger_db).mark_work(dj.id, src_work_id, "done")
                        await ledger_db.commit()
                    continue
```

- [ ] **Step 4: Replace the finalization block** (~720-779)

Find the block beginning `# ── Detect high skip rate` and ending at the parent-download `mark_source_sync_success` line (the whole `_high_skip_rate` computation, the `if ij:` status branch, and the parent-download branch, up to and including its `await db.commit()`). Replace the ENTIRE block with:

```python
        # ── Classify outcome (pure, unit-tested) ──
        total_groups = len(groups)
        async with async_session() as _cfg_db:
            _defaults = await get_download_defaults(_cfg_db)
        threshold = clamp_threshold(_defaults.get("import_skip_threshold", 0.5))
        status, message = classify_import_outcome(
            total_groups=total_groups,
            works=stats["works"],
            assets=stats["assets"],
            existing=stats["existing"],
            skipped=stats["skipped"],
            threshold=threshold,
        )

        async with async_session() as db:
            ij = await db.get(ImportJob, job_uuid)
            if ij:
                transition_import_job(ij, status, message if status == "failed" else None)
                apply_import_progress(
                    ij, status, message,
                    current=stats["works"], total=total_groups, assets=stats["assets"],
                )
                if status == "failed":
                    logger.warning("Import %s classified failed: %s", import_job_id, message)

                dj_repo = DownloadJobRepository(db)
                dj = await dj_repo.get(ij.download_job_id)
                if dj:
                    await dj_repo.update_status(dj, status, message if status == "failed" else None)
                    apply_download_progress(dj, status, message)
                    update_manifest(dj, import_stats=stats)
                    append_manifest_event(dj, "import_complete", status=status, **stats)
                    if status == "complete" and dj.subscription_source_id:
                        await mark_source_sync_success(db, dj.subscription_source_id)
                await db.commit()
```

- [ ] **Step 5: Verify nothing else referenced the removed names**

Run: `docker compose exec backend grep -n "_high_skip_rate\|skip_msg" backend/app/jobs/import_runner.py`
Expected: no output (all removed).

- [ ] **Step 6: Run the full suite (regression — the logic itself is unit-tested in Task 1)**

Run: `docker compose exec backend python -m pytest -q`
Expected: PASS (no failures). Investigate any failure before continuing.

- [ ] **Step 7: Commit**

```bash
git add backend/app/jobs/import_runner.py
git commit -m "fix(import): classify outcome via helper; count existing; fail on empty/all-skipped

Closes G1 (empty-result silent success) and G2 (no-op vs failure ambiguity).

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `classify_no_metadata_outcome` (pure helper, TDD)

**Files:**
- Create: `backend/app/jobs/download_outcome.py`
- Test: `backend/tests/test_download_outcome.py`

**Interfaces:**
- Produces: `classify_no_metadata_outcome(*, image_count: int, auth_warning: str | None, is_subscription: bool) -> tuple[str, str | None]` (status ∈ {"complete","failed"}, error message or None).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_download_outcome.py
from app.jobs.download_outcome import classify_no_metadata_outcome


def test_auth_warning_empty_is_failed():
    status, msg = classify_no_metadata_outcome(
        image_count=0, auth_warning="login required", is_subscription=True)
    assert status == "failed"
    assert "login required" in msg


def test_manual_empty_is_failed():
    status, msg = classify_no_metadata_outcome(
        image_count=0, auth_warning=None, is_subscription=False)
    assert status == "failed"
    assert "no artifacts" in msg


def test_subscription_empty_is_complete():
    status, msg = classify_no_metadata_outcome(
        image_count=0, auth_warning=None, is_subscription=True)
    assert status == "complete"
    assert msg is not None  # explains "no new content"


def test_media_without_metadata_is_complete():
    status, msg = classify_no_metadata_outcome(
        image_count=3, auth_warning=None, is_subscription=False)
    assert status == "complete"
    assert msg is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend python -m pytest backend/tests/test_download_outcome.py -v`
Expected: FAIL — `ModuleNotFoundError: app.jobs.download_outcome`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/jobs/download_outcome.py
"""Pure outcome classification for a download that wrote no new metadata JSONs."""
from __future__ import annotations


def classify_no_metadata_outcome(
    *,
    image_count: int,
    auth_warning: str | None,
    is_subscription: bool,
) -> tuple[str, str | None]:
    """Decide status when gallery-dl exits 0 but produced 0 new metadata JSONs.

    Returns (status, error_message); status in {"complete","failed"}.
      - auth warning + 0 media           -> failed (auth/restricted)
      - manual download + 0 media        -> failed (genuinely produced nothing)
      - subscription re-sync + 0 media   -> complete (no new content is normal)
      - media present but no metadata     -> complete (legacy behaviour preserved)
    """
    if image_count == 0 and auth_warning:
        return "failed", f"Download produced 0 files: {auth_warning}"
    if image_count == 0 and not is_subscription:
        return "failed", "gallery-dl exited 0 but produced no artifacts"
    if image_count == 0:
        return "complete", "No new content since last sync (already archived or empty)"
    return "complete", None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend python -m pytest backend/tests/test_download_outcome.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/download_outcome.py backend/tests/test_download_outcome.py
git commit -m "feat(download): pure classify_no_metadata_outcome helper with tests

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Wire empty-artifact outcome into `run_download_job`

**Files:**
- Modify: `backend/app/jobs/download.py` (the `else:` no-new-metadata branch, ~895-931)

**Interfaces:**
- Consumes: `classify_no_metadata_outcome` (Task 4).

- [ ] **Step 1: Add the import** (with the other `app.jobs` imports near the top)

```python
from app.jobs.download_outcome import classify_no_metadata_outcome
```

- [ ] **Step 2: Replace the status-decision in the `else` branch**

Find (~907-916):

```python
            # Determine status — then commit in single session
            if image_count == 0 and auth_warning:
                new_status = "failed"
                new_error = f"Download produced 0 files: {auth_warning}\n{stderr_text[:2000]}"
            elif image_count == 0:
                new_status = "complete"
                new_error = "Source has no downloadable content or all content is restricted"
            else:
                new_status = "complete"
                new_error = None
```

Replace with:

```python
            # Determine status via pure helper (manual empty -> failed; subscription
            # re-sync with no new content -> complete; see download_outcome.py).
            async with async_session() as _sub_db:
                _sub_job = await DownloadJobRepository(_sub_db).get(job_uuid)
                is_subscription = bool(_sub_job and _sub_job.subscription_source_id)
            new_status, new_error = classify_no_metadata_outcome(
                image_count=image_count,
                auth_warning=auth_warning,
                is_subscription=is_subscription,
            )
            if new_status == "failed" and auth_warning:
                new_error = f"{new_error}\n{stderr_text[:2000]}"
```

- [ ] **Step 3: Verify the downstream commit block still uses `new_status`/`new_error`**

Run: `docker compose exec backend grep -n "new_status\|new_error" backend/app/jobs/download.py`
Expected: the existing `db3` block (`repo3.update_status(j3, new_status, new_error)` and the `apply_download_progress(...)` using `new_status`) is unchanged and still present.

- [ ] **Step 4: Run the full suite**

Run: `docker compose exec backend python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/download.py
git commit -m "fix(download): manual download with 0 artifacts -> failed (subscriptions unaffected)

Closes G1 on the download side; refines spec decision #2 to spare subscription
re-syncs that legitimately have no new content.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Stop swallowing exceptions in the download path (G4)

**Files:**
- Modify: `backend/app/jobs/download.py` (proxy-health handlers ~458 and ~468-469; per-job config ~327-328)

- [ ] **Step 1: Fix the two proxy-health `pass` handlers**

Locate them: `docker compose exec backend grep -n "proxy:health" backend/app/jobs/download.py` then look at the `except Exception:` directly below each `hset`/`expire`. Each currently is:

```python
            except Exception:
                pass
```

Replace each of the two proxy-health occurrences with:

```python
            except Exception:
                logger.warning("Failed to record proxy health for %s", job.source, exc_info=True)
```

(Do NOT touch unrelated `pass` statements elsewhere in the file — only the two in the proxy-health blocks.)

- [ ] **Step 2: Raise the per-job config failure from debug to warning**

Find (~327-328):

```python
    except Exception:
        logger.debug("Failed to write per-job config for %s", job_id, exc_info=True)
```

Replace with:

```python
    except Exception:
        logger.warning("Failed to write per-job config for %s", job_id, exc_info=True)
```

- [ ] **Step 3: Confirm no silent proxy-health `pass` remains**

Run: `docker compose exec backend grep -n -A1 "proxy:health" backend/app/jobs/download.py`
Then visually confirm each surrounding `except` now logs. Expected: no silent `pass` in those handlers.

- [ ] **Step 4: Run the full suite**

Run: `docker compose exec backend python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/download.py
git commit -m "fix(download): log proxy-health and per-job-config failures (no silent pass)

Closes G4 (audit P1-5).

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Stage-timing helper + manifest wiring (G5)

**Files:**
- Create: `backend/app/jobs/stage_timing.py`
- Test: `backend/tests/test_stage_timing.py`
- Modify: `backend/app/jobs/download.py` (wrap the artifact-scan region)

**Interfaces:**
- Produces: `stage_timer(job, stage: str)` — a context manager that appends a `stage_timing` manifest event with `stage` and `ms`. Caller persists `job`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_stage_timing.py
from types import SimpleNamespace
from app.jobs.stage_timing import stage_timer


def test_stage_timer_appends_event():
    job = SimpleNamespace(manifest=None)
    with stage_timer(job, "scan"):
        pass
    events = job.manifest["events"]
    assert events[-1]["event"] == "stage_timing"
    assert events[-1]["stage"] == "scan"
    assert isinstance(events[-1]["ms"], int)
    assert events[-1]["ms"] >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec backend python -m pytest backend/tests/test_stage_timing.py -v`
Expected: FAIL — `ModuleNotFoundError: app.jobs.stage_timing`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/jobs/stage_timing.py
"""Time a pipeline stage and record it as a manifest event."""
from __future__ import annotations

import time
from contextlib import contextmanager

from app.services.job_manifest import append_manifest_event


@contextmanager
def stage_timer(job, stage: str):
    """Append a `stage_timing` event (stage, ms) to job.manifest on exit.

    The caller is responsible for persisting (committing) `job` afterwards.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        ms = int((time.monotonic() - start) * 1000)
        append_manifest_event(job, "stage_timing", stage=stage, ms=ms)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec backend python -m pytest backend/tests/test_stage_timing.py -v`
Expected: PASS.

- [ ] **Step 5: Wire one timing point into the download artifact scan**

In `backend/app/jobs/download.py`, add the import near the top:

```python
from app.jobs.stage_timing import stage_timer
```

Find the artifact-count block (~866):

```python
        metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
        async with async_session() as _manifest_db:
            _manifest_job = await DownloadJobRepository(_manifest_db).get(job_uuid)
            if _manifest_job:
                update_manifest(_manifest_job, metadata_json_count=metadata_count, image_count=image_count)
                append_manifest_event(_manifest_job, "artifacts_counted", metadata_json_count=metadata_count, image_count=image_count)
```

Replace with (time the scan on the same manifest job; keep `metadata_count`/`image_count`/`new_json_paths` defined on BOTH branches):

```python
        async with async_session() as _manifest_db:
            _manifest_job = await DownloadJobRepository(_manifest_db).get(job_uuid)
            if _manifest_job:
                with stage_timer(_manifest_job, "scan"):
                    metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
                update_manifest(_manifest_job, metadata_json_count=metadata_count, image_count=image_count)
                append_manifest_event(_manifest_job, "artifacts_counted", metadata_json_count=metadata_count, image_count=image_count)
            else:
                metadata_count, image_count, new_json_paths = await _artifact_counts(job_uuid)
```

(Leave the remaining lines of the original block — the `apply_download_progress(...)` call and the existing `await _manifest_db.commit()` — in place, still inside `if _manifest_job:`.)

- [ ] **Step 6: Run the full suite**

Run: `docker compose exec backend python -m pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/jobs/stage_timing.py backend/tests/test_stage_timing.py backend/app/jobs/download.py
git commit -m "feat(pipeline): stage_timer manifest events; time download artifact scan

Closes G5 (per-stage timing visibility).

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Full suite green: `docker compose exec backend python -m pytest -q`
- [ ] Deploy per Completion Workflow: `docker compose build --build-arg CACHEBUST="$(date +%s)" --pull backend && docker compose up -d --force-recreate backend worker-download worker-import scheduler`
- [ ] Smoke: a manual download of a known-empty/invalid URL → job shows `failed` with "no artifacts"; a subscription re-sync with no new posts → stays `complete`; a re-import of an already-imported creator → `complete` with `existing>0`, NOT a false `failed`.
- [ ] Confirm manifest shows `stage_timing` + `import_complete` (with `status`) events in the job detail view.

## Spec coverage check

- 3a import classification → Task 1 (helper) + Task 3 (wire) + `existing` counter ✓
- 3b download empty-artifact → Task 4 (helper) + Task 5 (wire, scoped to manual) ✓
- 3c configurable threshold → Task 2 + consumed in Task 3 ✓
- 3d swallowed exceptions → Task 6 ✓
- 3e stage timing → Task 7 ✓
- Testing (TDD) → Tasks 1, 4, 7 unit-tested; Task 2 tested; Tasks 3/5/6 regression-verified ✓
