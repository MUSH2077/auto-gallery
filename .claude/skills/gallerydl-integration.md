# gallery-dl Integration Skill

Guidance for integrating gallery-dl into the worker service. Read `.claude/constraints/docker.md`, `.claude/constraints/security.md`, and `.claude/constraints/filesystem-paths.md` before implementing.

## gallery-dl runtime

- gallery-dl is installed inside the backend Docker image (shared by `backend`, `worker`, `scheduler`)
- Only the `worker` service executes gallery-dl
- `backend` API enqueues download jobs; it never shells out

## Execution pattern (worker)

```python
import subprocess
import os
from pathlib import Path
from app.config import settings
from app.providers import registry

async def run_download_job(job_id: str):
    # 1. Load job and provider
    job = await repo.get(UUID(job_id))
    provider = registry.get(job.source)

    # 2. Load naming template and build per-job config
    naming_tpl = await load_naming_template(job.source)
    cfg = provider.build_gallerydl_config(None, naming_tpl)

    # 3. Write per-job config to GALLERYDL_CONFIG_ROOT/jobs/
    job_config_path = os.path.join(
        os.environ["GALLERYDL_CONFIG_ROOT"], "jobs", f"job-{job_id}.json")
    os.makedirs(os.path.dirname(job_config_path), exist_ok=True)
    with open(job_config_path, "w") as f:
        json.dump(cfg, f)

    # 4. Snapshot metadata JSONs before running (for new-file detection)
    json_before = _snapshot_metadata_jsons(job.source)

    # 5. Run gallery-dl
    config_path = os.path.join(
        os.environ["GALLERYDL_CONFIG_ROOT"], "config.json")

    cmd = ["gallery-dl", "--write-metadata"]
    if os.path.exists(config_path):
        cmd.extend(["--config", config_path])
    if job_config_path:
        cmd.extend(["--config", job_config_path])

    # Download archive prevents re-downloading already-seen URLs
    archive_path = os.path.join(
        str(settings.download_root), f"archive-{job.source}.sqlite3")
    cmd.extend(["--download-archive", archive_path])

    cmd.extend(["--destination", str(settings.download_root), job.source_url])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=dl_timeout)

    # 6. Detect new files via before/after diff (works regardless of naming template)
    metadata_count, image_count = _count_new_artifacts(job.source, json_before)

    # 7. Enqueue import only if new metadata JSONs were created
    if metadata_count > 0:
        await enqueue_import(job_id)
```

## Critical rules

### `shell=False` (non-negotiable)

- Always pass the command as `list[str]`, never as a single string
- Never use `shell=True`
- Validate the URL through `provider.validate_url()` before job creation — the worker must never receive an unvalidated URL

### Naming templates determine directory structure

The actual file tree under `DOWNLOAD_ROOT/{source}/` is determined by the naming template's `directory` config. NEVER assume a fixed path like `{source}/{creator}/{work_id}/`. See `.claude/constraints/filesystem-paths.md`.

### Before/after snapshots for new-file detection

gallery-dl exit code 0 does NOT mean files were downloaded (content may already be in the download archive). Use `_snapshot_metadata_jsons()` before and `_count_new_artifacts()` after to detect actual new files. See `backend/app/jobs/download.py` for the current implementation.

### Download archive

gallery-dl uses `--download-archive` (SQLite file per source at `DOWNLOAD_ROOT/archive-{source}.sqlite3`) to skip already-downloaded URLs. This means:
- Re-running a job for the same URL is safe — no duplicates
- Exit code 0 is ambiguous: it means success, not "downloaded something"
- The archive file is source-scoped, not subscription-scoped

## Config management

- gallery-dl base config lives at `$GALLERYDL_CONFIG_ROOT/config.json`
- Cookies/authentication files live at `$GALLERYDL_CONFIG_ROOT/cookies/`
- Per-job configs are generated from `build_gallerydl_config()` and written to `$GALLERYDL_CONFIG_ROOT/jobs/<job_id>.json`
- Temporary job configs are cleaned up after job completion
- Per-source `auto_enable_on_import` is stored in `config.json` under each extractor, NOT in DB system_settings

## Download flow

1. Scheduler (or manual trigger) creates a `download_job` record with status `pending`
2. RQ worker picks up `pending` jobs from the queue
3. Worker updates job status to `downloading`
4. Worker calls `provider.build_gallerydl_config(None, naming_template)` to generate per-job gallery-dl config
5. Worker writes the temp config to `GALLERYDL_CONFIG_ROOT/jobs/<job_id>.json`
6. Worker snapshots metadata JSONs via `_snapshot_metadata_jsons(source)` (before/after diff)
7. Worker runs `gallery-dl` — output directory is determined by the naming template
8. Worker detects new files via `_count_new_artifacts(source, json_before)` — before/after diff
9. If new JSONs found: update job status to `downloaded`, enqueue import job
10. If no new JSONs found: update job status to `complete` with explanation (or `failed` if stderr indicates auth error)
11. On error: update job status to `failed`/`pending` (retry), persist stderr to `download_job.error_log`

## Output

gallery-dl writes files according to the naming template's `directory` config (set via per-job config). The actual path is determined by the template — DO NOT assume `{source}/{creator}/{work_id}/`. The import job then:
- Scans `DOWNLOAD_ROOT/{source}/` recursively for `.json` metadata files written by `--write-metadata`
- Uses `download_dir` as a hint to narrow the scan, with fallback to full source root
- Groups JSONs by `source_work_id` (extracted via `provider.parse_work_source()`)
- Creates DB records (Work, WorkSource, Asset, AssetSource, Tags)
- Generates thumbnails (pyvips WebP) in `LIBRARY_ROOT/{source}/{creator_dir}/{work_id}/`
- Writes `metadata.json` to `LIBRARY_ROOT/{source}/{creator_dir}/{work_id}/`
- Deletes processed JSON files (image files stay in DOWNLOAD_ROOT)
- Marks parent `download_job` as `complete` when import finishes

## Timeout and retry

- Default timeout: 600 seconds per job (configurable via `download_defaults.timeout_seconds`)
- RQ job timeout: `job_timeout=7200` (2 hours) on all enqueue calls — RQ defaults to 180s otherwise
- Max retries: 3 per job (configurable via `download_defaults.max_retries`)
- Retry with exponential backoff (base configurable via `download_defaults.retry_backoff_base_seconds`)
- Jobs stuck in `downloading` for > 2x timeout are marked `stale` by the scheduler
- Partial import recovery: if metadata JSONs exist but the job failed, a partial import is attempted

## Job state machines (canonical)

Two separate lifecycles — `download_job` and `import_job`. This is the single source of truth.

### download_job

```
pending
  → downloading    (worker picked up the job, started gallery-dl)
  → downloaded     (gallery-dl completed, new metadata JSONs found, import enqueued)
  → complete       (no new JSONs — all content already in archive, OR import finished successfully)
  → failed         (gallery-dl or import failed after max retries, error_log populated)
  → stale          (stuck in downloading for > 2x timeout)
  → paused         (user paused — job skipped during auto-sync scans)
```

Transitions:
- `pending → downloading`: worker dequeues job, updates status before gallery-dl starts
- `downloading → downloaded`: gallery-dl exit 0 AND new JSONs detected
- `downloading → complete`: gallery-dl exit 0 but no new JSONs (all content in archive)
- `downloading → pending`: non-zero exit, retry_count < max_retries, auto-re-enqueued with backoff
- `downloading → failed`: non-zero exit, retry_count >= max_retries, stderr captured
- `downloaded → complete`: import job finished, import_runner marks download_job as complete
- `downloading → stale`: scheduler detects no progress for > 2x timeout
- Any state → `paused`: user manually pauses

### import_job

```
pending
  → running        (import runner started processing)
  → complete       (all metadata JSONs processed, DB records created, thumbnails generated)
  → failed         (error after retry exhausted, error_log populated)
```

Transitions:
- `pending → running`: import runner dequeues job
- `running → complete`: all works imported successfully
- `pending → pending`: first failure — auto-retry once with 60s backoff (RETRY_ATTEMPT marker in error_log)
- `pending → failed`: second failure — permanent
- `running → failed`: unrecoverable error during processing

## Auth health tracking

On `subscription_source`:
- `last_successful_auth`: timestamp of last download that didn't fail with auth error
- After any download failure: check stderr for auth-related keywords (`401`, `403`, "login", "authentication", "forbidden")
- If auth failure detected: mark `auth_healthy = False`
- On next successful download: mark `auth_healthy = True`, update `last_successful_auth`
- Auth health is checked by the scheduler — sources with `auth_healthy = False` are skipped during auto-sync
- Admin cookie/config status page queries `auth_healthy` per subscription source

## Worker startup orphan reconciliation

On worker startup, before processing any new jobs:

1. Query `download_job` for jobs with status `downloading` or `importing`
2. Mark them `stale` (worker died mid-job)
3. Scan `DOWNLOAD_ROOT/{source}/` directories
4. For any directory without a corresponding `downloaded` or `importing` job:
   - Create a `download_job` with status `downloaded` (orphaned download)
   - Enqueue an import job

This ensures no downloaded files are silently lost on worker restart.

## Debugging download/import failures

### 1. Check which queue the job went to

```bash
# Auto-sync jobs go to "scheduled" queue
docker compose logs scheduler | grep "run_download_job"

# Manual retry jobs go to "default" queue  
docker compose logs worker | grep "run_download_job"
```

### 2. Inspect the job in the database

```bash
docker compose exec backend python3 -c "
import asyncio
from app.database import async_session
from app.models.download_job import DownloadJob
from app.models.import_job import ImportJob
from sqlalchemy import select

async def check():
    async with async_session() as db:
        r = await db.execute(select(DownloadJob).where(DownloadJob.id == 'UUID'))
        dj = r.scalar_one_or_none()
        if dj:
            print(f'status={dj.status} error={dj.error_log[:500]}')
        
        r2 = await db.execute(select(ImportJob).where(ImportJob.download_job_id == dj.id))
        for ij in r2.scalars():
            print(f'import: status={ij.status} error={ij.error_log[:500] if ij.error_log else None}')
asyncio.run(check())
"
```

### 3. Verify the gallery-dl config is correct

```bash
# Check what gallery-dl actually sees
docker compose exec worker python3 -c "
import json
with open('/gallerydl-config/config.json') as f:
    c = json.load(f)
print('Pixiv extractor:', json.dumps(c.get('extractor',{}).get('pixiv',{}), indent=2))
print('Postprocessors:', json.dumps(c.get('postprocessors',[]), indent=2))
"
```

### 4. Test gallery-dl directly with the production config

```bash
docker compose exec worker gallery-dl --config /gallerydl-config/config.json \
  --destination /tmp/test --range 1-1 "<url>" -v 2>&1 | grep -i "error\|ugoira\|postprocessor\|active"
```

### 5. Inspect downloaded metadata

```bash
docker compose exec worker python3 -c "
import json, glob
for f in glob.glob('/tmp/test/pixiv/**/*.json', recursive=True):
    with open(f) as jf:
        meta = json.load(jf)
    print(f'{f}: rating={meta.get(\"rating\")} x_restrict={meta.get(\"x_restrict\")} type={meta.get(\"type\")}')
"
```

### 6. Common failure patterns

| Symptom | Likely cause | Check |
|---------|-------------|-------|
| Download "complete" in 5s, no files | snapshot falsely detected JSONs, or all in archive | Check `error_log`, check download archive |
| Import "no metadata JSON files" | scan_root mismatch (naming template vs download_dir) | Check filesystem for actual JSON locations |
| Ugoira saved as ZIP not GIF | `--write-metadata` conflict OR missing postprocessor | Check config postprocessors, remove `--write-metadata` |
| NSFW not detected | Using wrong metadata field | Read `metadata-detection.md` constraint |
| Worker not processing new code | Container not recreated after image rebuild | Check container creation timestamp |

### 7. Manually trigger a download for testing

```bash
docker compose exec backend python3 -c "
import asyncio, redis as redis_lib
from app.database import async_session
from app.models.subscription_source import SubscriptionSource
from app.models.download_job import DownloadJob
from app.config import settings
from rq import Queue
from sqlalchemy import select

async def trigger():
    async with async_session() as db:
        r = await db.execute(select(SubscriptionSource).where(
            SubscriptionSource.source == 'pixiv', SubscriptionSource.is_enabled == True).limit(1))
        ss = r.scalar_one_or_none()
        if ss:
            job = DownloadJob(subscription_id=ss.subscription_id, subscription_source_id=ss.id,
                             source=ss.source, source_url=ss.source_url, status='pending')
            db.add(job)
            await db.flush()
            red = redis_lib.from_url(settings.redis_url)
            Queue(name='scheduled', connection=red).enqueue(
                'app.jobs.download.run_download_job', str(job.id), job_timeout=7200)
            await db.commit()
            print(f'Enqueued job {job.id} for {ss.source_url}')
asyncio.run(trigger())
"
```
