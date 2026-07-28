# Disk-Import & Creator-Relink Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add (A) an automatic reconcile that links orphaned `source_creator` rows to their creator so imported works always appear on the creator page, and (B) an idempotent "import from disk" operation (backend job + admin endpoint + frontend button) that imports gallery-dl files already on disk into the DB without re-downloading or deleting them.

**Architecture:** Both features reuse the existing PostgreSQL `storage_artifacts` ledger + `run_import_job` pipeline. (A) extracts the relink logic into a shared service called from the scheduler. (B) scans `DOWNLOAD_ROOT`, registers un-imported metadata files under a synthetic "recovery" `download_job`, and enqueues the normal import job — idempotent via the ledger (`upsert_many` mtime guard) and import-side `claim_work` leasing.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, RQ (operations queue), Next.js 14 + TanStack Query, pytest.

## Global Constraints

- Layered architecture: route (thin) → service → repository → model. No business logic in routes.
- `shell=False` everywhere; no new subprocess use in this plan.
- All admin APIs require `RequireAdmin` (mirror existing admin routes).
- Idempotent: re-running any operation must NOT create duplicate works (rely on ledger `upsert_many` conflict handling + import `claim_work`).
- Never expose NAS filesystem paths to clients (operations return counts/job_id only).
- Tests run via `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest ...` (backend image is baked; mount local source to test edits without rebuild).
- Frontend i18n keys are natural-language; add both `zh` and `en`.
- Commit after each task. End commit messages with `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

## File Structure

- `backend/app/services/creator_reconcile.py` (NEW) — shared relink service used by scheduler + recovery script.
- `backend/app/jobs/subscription_sync.py` (MODIFY) — call reconcile at end of scan.
- `backend/recover_sync_state.py` (MODIFY) — use the shared service (DRY).
- `backend/app/services/disk_import.py` (NEW) — scan-disk → register ledger → enqueue import.
- `backend/app/jobs/admin_operations.py` (MODIFY) — `run_disk_import_operation` RQ entry.
- `backend/app/api/admin.py` (MODIFY) — `POST /library/import-from-disk` + `ImportFromDiskRequest`.
- `backend/tests/test_creator_reconcile.py` (NEW), `backend/tests/test_disk_import.py` (NEW).
- `admin-web/src/lib/api/index.ts` (MODIFY) — `importFromDisk()` client call.
- `admin-web/src/app/admin/data-mgmt/page.tsx` (MODIFY) — "Import from disk" button + polling.
- `admin-web/src/lib/i18n/*` (MODIFY) — button/label keys.

---

## Task 1: Shared creator-relink reconcile service

**Files:**
- Create: `backend/app/services/creator_reconcile.py`
- Test: `backend/tests/test_creator_reconcile.py`

**Interfaces:**
- Produces: `async def reconcile_unlinked_source_creators(db: AsyncSession, source: str | None = None) -> dict` returning `{"linked": int, "skipped": int}`. Links each `SourceCreator` with `creator_id IS NULL` to the unique creator of a single-creator subscription whose URL identity (`provider.get_creator_dir_from_url`) equals the source_creator's `source_creator_id`. Skips when 0 or >1 candidate creators (ambiguous → manual review). Flushes; never commits (caller owns the transaction).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_creator_reconcile.py
import pytest

from app.services.creator_reconcile import reconcile_unlinked_source_creators


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relinks_single_creator_subscription():
    from app.database import async_session
    from app.models.creator import Creator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.models.source_creator import SourceCreator

    async with async_session() as db:
        creator = Creator(display_name="Machi")
        db.add(creator); await db.flush()
        sub = Subscription(creator_id=creator.id, name="Machi")
        db.add(sub); await db.flush()
        ss = SubscriptionSource(subscription_id=sub.id, source="pixiv",
                                source_url="https://www.pixiv.net/users/4632856")
        db.add(ss)
        sc = SourceCreator(source="pixiv", source_creator_id="4632856",
                           display_name="Machi", creator_id=None)
        db.add(sc); await db.flush()

        result = await reconcile_unlinked_source_creators(db, source="pixiv")
        await db.refresh(sc)

        assert result["linked"] >= 1
        assert sc.creator_id == creator.id
        await db.rollback()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest tests/test_creator_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.creator_reconcile'`.

- [ ] **Step 3: Write the service**

```python
# backend/app/services/creator_reconcile.py
"""Link orphaned SourceCreators (creator_id IS NULL) to their subscription's
creator by identity match. Mirrors import_runner._should_auto_link_creator so a
bookmark/repost feed (ambiguous identity) is never mislinked."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry


async def reconcile_unlinked_source_creators(db: AsyncSession, source: str | None = None) -> dict:
    q = select(SourceCreator).where(SourceCreator.creator_id.is_(None))
    if source:
        q = q.where(SourceCreator.source == source)
    unlinked = (await db.execute(q)).scalars().all()

    linked = skipped = 0
    for sc in unlinked:
        try:
            provider = registry.get(sc.source)
        except KeyError:
            skipped += 1
            continue
        ss_rows = (await db.execute(
            select(SubscriptionSource).where(SubscriptionSource.source == sc.source)
        )).scalars().all()
        candidates: set = set()
        for ss in ss_rows:
            if not ss.source_url or not ss.subscription_id:
                continue
            url_id = provider.get_creator_dir_from_url(ss.source_url)
            if url_id is not None and str(url_id) == str(sc.source_creator_id):
                sub = await db.get(Subscription, ss.subscription_id)
                if sub and sub.creator_id:
                    candidates.add(sub.creator_id)
        if len(candidates) == 1:
            sc.creator_id = next(iter(candidates))
            linked += 1
        else:
            skipped += 1
    if linked:
        await db.flush()
    return {"linked": linked, "skipped": skipped}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest tests/test_creator_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/creator_reconcile.py backend/tests/test_creator_reconcile.py
git commit -m "feat(creator): shared reconcile_unlinked_source_creators service"
```

---

## Task 2: Hook reconcile into the scheduler; DRY the recovery script

**Files:**
- Modify: `backend/app/jobs/subscription_sync.py` (after the auto-sync scan, before the re-schedule block near the end)
- Modify: `backend/recover_sync_state.py` (replace inline relink with the shared service)

**Interfaces:**
- Consumes: `reconcile_unlinked_source_creators(db, source=None)` from Task 1.

- [ ] **Step 1: Add the reconcile call to the scheduler**

In `backend/app/jobs/subscription_sync.py`, immediately BEFORE the `# Re-schedule for next scan` block at the end of `sync_subscriptions`, add:

```python
    # Self-heal: link any orphaned source_creators to their creator so imported
    # works always surface on the creator page (cheap; usually 0 rows).
    try:
        from app.services.creator_reconcile import reconcile_unlinked_source_creators
        async with async_session() as relink_db:
            res = await reconcile_unlinked_source_creators(relink_db)
            if res["linked"]:
                await relink_db.commit()
                logger.info("Auto-sync relinked %d orphaned source_creators", res["linked"])
    except Exception:
        logger.debug("source_creator reconcile skipped", exc_info=True)
```

- [ ] **Step 2: DRY the recovery script**

In `backend/recover_sync_state.py`, replace the body of `relink_source_creators` with a call to the shared service (keeping the dry-run reporting):

```python
async def relink_source_creators(db) -> tuple[int, int]:
    """Relink unlinked SourceCreators via the shared reconcile service."""
    from app.services.creator_reconcile import reconcile_unlinked_source_creators
    if not APPLY:
        # Dry-run: count what WOULD link without writing.
        before = await reconcile_unlinked_source_creators(db, source=None)
        await db.rollback()
        print(f"  would link {before['linked']}, skip {before['skipped']}")
        return before["linked"], before["skipped"]
    res = await reconcile_unlinked_source_creators(db)
    print(f"  linked {res['linked']}, skipped {res['skipped']}")
    return res["linked"], res["skipped"]
```

- [ ] **Step 3: Verify scheduler module imports and recovery script parses**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -c "import app.jobs.subscription_sync; import ast; ast.parse(open('recover_sync_state.py').read()); print('OK')"`
Expected: prints `OK`.

- [ ] **Step 4: Run reconcile + state tests**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest tests/test_creator_reconcile.py tests/test_job_state_manifest.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/subscription_sync.py backend/recover_sync_state.py
git commit -m "feat(scheduler): auto-relink orphaned source_creators each sync scan"
```

---

## Task 3: Disk-import service (scan → register ledger → enqueue import)

**Files:**
- Create: `backend/app/services/disk_import.py`
- Test: `backend/tests/test_disk_import.py`

**Interfaces:**
- Consumes: `artifact_row`, `ArtifactLedger.upsert_many` (`app.services.artifact_ledger`); `_enqueue_import` (`app.jobs.download`); `registry` (`app.providers`).
- Produces: `async def reconcile_downloads_to_db(db: AsyncSession, options: dict, progress_callback=None) -> dict` returning `{"sources": int, "creators": int, "jobs": int, "skipped_done": int}`. For each source dir under `DOWNLOAD_ROOT` (filtered by `options["source"]` when set), groups on-disk metadata JSONs (excluding those already `state='done'` in the ledger) by creator_dir; for each group it creates a recovery `DownloadJob(status="downloaded")` (linked to the matching single-creator subscription when identity resolves), registers the JSON + sibling image artifacts under it, and enqueues an import via `_enqueue_import`. Idempotent.

- [ ] **Step 1: Write the failing test (signature contract)**

```python
# backend/tests/test_disk_import.py
def test_reconcile_downloads_to_db_is_importable():
    # Contract smoke test: the service module exposes the entrypoint with the
    # documented signature. Behavioural coverage is via integration runs.
    from app.services.disk_import import reconcile_downloads_to_db
    import inspect
    sig = inspect.signature(reconcile_downloads_to_db)
    assert list(sig.parameters)[:2] == ["db", "options"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest tests/test_disk_import.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.disk_import'`.

- [ ] **Step 3: Write the service**

```python
# backend/app/services/disk_import.py
"""Import gallery-dl files already on disk into the DB without re-downloading.

Scans DOWNLOAD_ROOT/{source}/, registers metadata + image artifacts that are not
yet imported (ledger state != 'done') under a synthetic 'recovery' download_job,
and enqueues the normal import pipeline. Idempotent: the ledger upsert mtime
guard keeps already-'done' files untouched and import-side claim_work dedups
works, so re-running never produces duplicates and never deletes/re-downloads."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_job import DownloadJob
from app.models.storage_artifact import StorageArtifact
from app.models.subscription_source import SubscriptionSource
from app.providers import registry
from app.services.artifact_ledger import ArtifactLedger, artifact_row

logger = logging.getLogger(__name__)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip"}


async def _match_subscription_source(db: AsyncSession, source: str, source_creator_id: str):
    """Find the single-creator subscription_source whose URL identity matches."""
    try:
        provider = registry.get(source)
    except KeyError:
        return None
    ss_rows = (await db.execute(
        select(SubscriptionSource).where(SubscriptionSource.source == source)
    )).scalars().all()
    for ss in ss_rows:
        if ss.source_url and str(provider.get_creator_dir_from_url(ss.source_url)) == str(source_creator_id):
            return ss
    return None


async def reconcile_downloads_to_db(db: AsyncSession, options: dict, progress_callback=None) -> dict:
    from app.jobs.download import _enqueue_import

    root = Path(str(settings.download_root))
    source_filter = options.get("source")
    sources = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            if source_filter and d.name != source_filter:
                continue
            try:
                registry.get(d.name)
            except KeyError:
                continue
            sources.append(d.name)

    stats = {"sources": 0, "creators": 0, "jobs": 0, "skipped_done": 0}
    for source in sources:
        stats["sources"] += 1
        provider = registry.get(source)
        scan_root = root / source

        done_paths = set((await db.execute(
            select(StorageArtifact.file_path).where(
                StorageArtifact.source == source, StorageArtifact.state == "done")
        )).scalars())

        groups: dict[str, list[Path]] = defaultdict(list)
        for jf in scan_root.rglob("*.json"):
            if not jf.is_file() or jf.parent == scan_root:
                continue
            rel = jf.relative_to(root)
            if len(rel.parts) < 3:
                continue
            if str(rel) in done_paths:
                stats["skipped_done"] += 1
                continue
            groups[rel.parts[1]].append(jf)

        total = len(groups)
        for i, (creator_dir, jsons) in enumerate(sorted(groups.items())):
            # Resolve identity from the first JSON to link the recovery job.
            ss = None
            try:
                with open(jsons[0]) as f:
                    first_raw = json.load(f)
                sc_id = provider.parse_source_creator(first_raw).get("source_creator_id")
                if sc_id:
                    ss = await _match_subscription_source(db, source, sc_id)
            except Exception:
                logger.warning("disk_import: could not read identity for %s/%s", source, creator_dir, exc_info=True)

            job = DownloadJob(
                source=source,
                source_url=(ss.source_url if ss else f"recovery://{source}/{creator_dir}"),
                status="downloaded",
                subscription_id=(ss.subscription_id if ss else None),
                subscription_source_id=(ss.id if ss else None),
            )
            db.add(job)
            await db.flush()

            rows = []
            seen = set()
            new_paths = set()
            for jf in jsons:
                for af in jf.parent.iterdir():
                    if af.is_file() and af.suffix.lower() in IMG_EXTS:
                        ar = artifact_row(af, root, job.id)
                        if ar and ar["file_path"] not in seen:
                            seen.add(ar["file_path"]); rows.append(ar)
                jr = artifact_row(jf, root, job.id)
                if jr and jr["file_path"] not in seen:
                    seen.add(jr["file_path"]); rows.append(jr); new_paths.add(str(jf))
            await ArtifactLedger(db).upsert_many(rows)
            await db.commit()

            await _enqueue_import(str(job.id), new_json_paths=new_paths)
            stats["creators"] += 1
            stats["jobs"] += 1
            if progress_callback:
                progress_callback({"phase": "running", "scanned": i + 1, "total": total, "source": source})

    return stats
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest tests/test_disk_import.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/disk_import.py backend/tests/test_disk_import.py
git commit -m "feat(import): disk_import service — import on-disk files without re-download"
```

---

## Task 4: RQ operation + admin endpoint

**Files:**
- Modify: `backend/app/jobs/admin_operations.py` (add `run_disk_import_operation`, mirroring `run_library_rebuild_operation`)
- Modify: `backend/app/api/admin.py` (add `ImportFromDiskRequest` + `POST /library/import-from-disk`, mirroring `rebuild_library`)

**Interfaces:**
- Consumes: `reconcile_downloads_to_db` (Task 3); `set_operation_status`, `get_operation_status` (`app.services.operations`).
- Produces: endpoint returns `{"status": "enqueued", "job_id": str, "message": str, "options": dict}`. RQ entry `run_disk_import_operation(job_id: str, options: dict) -> dict`.

- [ ] **Step 1: Add the RQ operation**

Append to `backend/app/jobs/admin_operations.py`:

```python
def run_disk_import_operation(job_id: str, options: dict | None = None) -> dict:
    """Entry point for RQ workers — import on-disk download files into the DB."""
    return asyncio.run(_run_disk_import_operation(job_id, options or {}))


async def _run_disk_import_operation(job_id: str, options: dict) -> dict:
    from app.services.disk_import import reconcile_downloads_to_db
    set_operation_status(job_id, "running", "admin-disk-import",
        progress={"phase": "running", "label": "Scanning download root..."},
        meta={"entity": "disk-import", **options})
    try:
        def update_progress(progress: dict):
            set_operation_status(job_id, "running", "admin-disk-import",
                progress={**progress, "label": f"Imported {progress.get('scanned', 0)} of {progress.get('total', 0)} creators"},
                meta={"entity": "disk-import", **options})

        async with async_session() as db:
            result = await reconcile_downloads_to_db(db, options, update_progress)
        set_operation_status(job_id, "complete", "admin-disk-import",
            progress={"phase": "complete", "label": f"Queued {result['jobs']} import jobs"},
            result=result, meta={"entity": "disk-import", **options})
        return result
    except Exception as exc:
        logger.exception("Disk import failed: job_id=%s", job_id)
        set_operation_status(job_id, "failed", "admin-disk-import",
            progress={"phase": "failed"}, error=str(exc), meta={"entity": "disk-import", **options})
        raise
    finally:
        from app.services.redis_client import get_redis
        get_redis().delete("library:disk-import:active")
```

- [ ] **Step 2: Add the endpoint**

In `backend/app/api/admin.py`, after the `rebuild_library` route, add:

```python
class ImportFromDiskRequest(BaseModel):
    source: str | None = None


@router.post("/library/import-from-disk")
async def import_from_disk(data: ImportFromDiskRequest | None = None):
    """Enqueue an idempotent import of on-disk download files into the DB."""
    from rq import Queue
    import uuid
    from app.services.redis_client import get_redis
    from app.services.operations import set_operation_status

    options = (data or ImportFromDiskRequest()).model_dump(mode="json")
    redis = get_redis()
    job_id = str(uuid.uuid4())
    active_job = redis.get("library:disk-import:active")
    if isinstance(active_job, bytes):
        active_job = active_job.decode()
    if active_job:
        active_status = get_operation_status(active_job)
        if not active_status or active_status.get("status") in {"complete", "failed", "cancelled"}:
            redis.delete("library:disk-import:active")
    if not redis.set("library:disk-import:active", job_id, nx=True, ex=604800):
        active_job = redis.get("library:disk-import:active")
        if isinstance(active_job, bytes):
            active_job = active_job.decode()
        raise HTTPException(status_code=409, detail={"message": "Disk import already running", "job_id": active_job})
    set_operation_status(job_id, "enqueued", "admin-disk-import",
        progress={"phase": "enqueued", "label": "Disk import queued"},
        meta={"entity": "disk-import", **options})

    Queue(name="operations", connection=redis).enqueue(
        "app.jobs.admin_operations.run_disk_import_operation",
        job_id, options, job_timeout=14400, result_ttl=604800)

    return {"status": "enqueued", "job_id": job_id, "message": "Disk import queued", "options": options}
```

- [ ] **Step 3: Verify modules import**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -c "import app.jobs.admin_operations, app.api.admin; print('OK')"`
Expected: prints `OK`.

- [ ] **Step 4: Run backend suite (no regressions)**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest -q -m "not integration"`
Expected: PASS (all non-integration tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/jobs/admin_operations.py backend/app/api/admin.py
git commit -m "feat(admin): POST /library/import-from-disk operation"
```

---

## Task 5: Client API + frontend button + i18n

**Files:**
- Modify: `admin-web/src/lib/api/index.ts` (add `importFromDisk`)
- Modify: `admin-web/src/app/admin/data-mgmt/page.tsx` (button in the Cleanup Tools card)
- Modify: `admin-web/src/lib/i18n/zh.ts` and `admin-web/src/lib/i18n/en.ts` (keys)

**Interfaces:**
- Consumes: `POST /api/v1/admin/library/import-from-disk` (Task 4); existing `getOperationStatus(jobId)` for polling.

- [ ] **Step 1: Add the client call**

In `admin-web/src/lib/api/index.ts`, after `rebuildLibrary`, add:

```ts
  importFromDisk: (options: { source?: string } = {}) =>
    request<{ job_id: string; status: string; message: string }>("/api/v1/admin/library/import-from-disk", {
      method: "POST",
      body: JSON.stringify(options),
    }),
```

- [ ] **Step 2: Add the button + mutation**

In `admin-web/src/app/admin/data-mgmt/page.tsx`, add a mutation near the other mutations (e.g. beside `cleanupJSON`):

```tsx
  const importFromDisk = useMutation({
    mutationFn: () => api.importFromDisk({}),
  });
```

And add a row inside the Cleanup Tools card's `space-y-3` container, after the reindex row:

```tsx
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.disk_import")}</p>
                <p className="text-xs text-muted">{t("datamgmt.disk_import_desc")}</p>
              </div>
              <button onClick={() => importFromDisk.mutate()} disabled={importFromDisk.isPending}
                className="btn-primary shrink-0 ml-3 text-xs">
                {importFromDisk.isPending ? "..." : t("datamgmt.disk_import_btn")}
              </button>
            </div>
```

- [ ] **Step 3: Add i18n keys**

In `admin-web/src/lib/i18n/zh.ts` add (in the `datamgmt` group):

```ts
  "datamgmt.disk_import": "从下载目录导入",
  "datamgmt.disk_import_desc": "扫描下载目录，把已在盘上但未入库的文件导入数据库（幂等，不重新下载/删除）",
  "datamgmt.disk_import_btn": "从盘导入",
```

In `admin-web/src/lib/i18n/en.ts` add:

```ts
  "datamgmt.disk_import": "Import from disk",
  "datamgmt.disk_import_desc": "Scan the download directory and import on-disk-but-unindexed files into the database (idempotent; no re-download/delete)",
  "datamgmt.disk_import_btn": "Import",
```

- [ ] **Step 4: Build admin-web (typecheck + compile)**

Run: `docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web`
Expected: `BUILD OK` (Next build incl. typecheck passes). If the i18n files are typed maps, ensure the new keys exist in BOTH `zh.ts` and `en.ts` or the typecheck fails.

- [ ] **Step 5: Commit**

```bash
git add admin-web/src/lib/api/index.ts admin-web/src/app/admin/data-mgmt/page.tsx admin-web/src/lib/i18n/zh.ts admin-web/src/lib/i18n/en.ts
git commit -m "feat(admin-web): Import-from-disk button in Data Management"
```

---

## Task 6: Full verification + deploy notes

**Files:** none (verification only).

- [ ] **Step 1: Full backend suite**

Run: `docker compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest -q`
Expected: PASS (integration tests run against the provisioned `*_test` DB).

- [ ] **Step 2: Rebuild + redeploy stack**

Run:
```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" --pull backend admin-web
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler admin-web
```
Expected: containers recreated; `docker compose logs worker-operations --tail=20` shows the worker ready.

- [ ] **Step 3: Smoke test the endpoint**

Run:
```bash
curl -s -X POST -H "Authorization: Bearer <admin-jwt>" -H "Content-Type: application/json" \
  -d '{"source":"pixiv"}' http://localhost:8818/api/v1/admin/library/import-from-disk
```
Expected: JSON `{"status":"enqueued","job_id":"..."}`. Then `docker compose logs worker-operations --tail=40` shows `admin-disk-import` running and queueing import jobs; creator pages for previously-empty creators populate as imports complete.

- [ ] **Step 4: Commit any doc updates**

```bash
git add -A && git commit -m "docs: disk-import + creator-relink verified" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- A (auto relink): Task 1 (service) + Task 2 (scheduler hook + script DRY). ✓
- B (import on-disk without re-download): Task 3 (service) + Task 4 (job/endpoint) + Task 5 (frontend). ✓
- Download/import already separate: documented; B reuses `run_import_job` via `_enqueue_import`. ✓
- Idempotency: ledger `upsert_many` mtime guard + `claim_work` + skip `state='done'` in Task 3. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. ✓

**Type consistency:** `reconcile_unlinked_source_creators(db, source)` → `{"linked","skipped"}` used in Task 2. `reconcile_downloads_to_db(db, options, progress_callback)` → `{"sources","creators","jobs","skipped_done"}` used in Task 4. `_enqueue_import(download_job_id, new_json_paths=...)` matches `app/jobs/download.py`. `set_operation_status`/`get_operation_status` match `app/services/operations.py`. ✓
