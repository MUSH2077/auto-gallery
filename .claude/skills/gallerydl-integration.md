# gallery-dl Integration Skill

Guidance for integrating gallery-dl into the worker service. Read `.claude/constraints/docker.md` and `.claude/constraints/security.md` before implementing.

## gallery-dl runtime

- gallery-dl is installed inside the backend Docker image (shared by `backend`, `worker`, `scheduler`)
- Only the `worker` service executes gallery-dl
- `backend` API enqueues download jobs; it never shells out

## Execution pattern (worker)

```python
import subprocess
import json
from pathlib import Path

def run_gallerydl_download(job_config: dict, url: str) -> subprocess.CompletedProcess:
    config_path = write_temp_config(job_config)
    download_dir = Path(os.environ["DOWNLOAD_ROOT"]) / str(job_config["job_id"])

    cmd = [
        "gallery-dl",
        "--config", str(config_path),
        "--destination", str(download_dir),
        "--write-metadata",
        "--write-info-json",
        url
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise DownloadError(
            job_id=job_config["job_id"],
            stderr=result.stderr,
            returncode=result.returncode,
        )

    return result
```

## Critical: shell=False

- Always pass the command as `list[str]`, never as a single string
- Never use `shell=True`
- Validate the URL through the provider's `validate_url()` before the job is even created — the worker should never receive an unvalidated URL

## Config management

- gallery-dl base config lives at `$GALLERYDL_CONFIG_ROOT/config.json`
- Cookies/authentication files live at `$GALLERYDL_CONFIG_ROOT/cookies/`
- Per-job configs are generated from `build_gallerydl_config()` and written to `$GALLERYDL_CONFIG_ROOT/jobs/<job_id>.json`
- Temporary job configs are cleaned up after job completion

## Download flow

1. Scheduler (or manual trigger) creates a `download_job` record with status `pending`
2. Worker picks up `pending` jobs from the queue
3. Worker calls `provider.build_gallerydl_config(subscription_source, naming_template)` to generate the job-specific gallery-dl config
4. Worker writes the temp config to `GALLERYDL_CONFIG_ROOT/jobs/<job_id>.json`
5. Worker runs `gallery-dl` with the temp config, writing directly to `DOWNLOAD_ROOT/{source}/{creator}/{work_id}/` (directory structure set by the provider's gallery-dl config template)
6. On success: update job status to `downloaded`, enqueue import job
7. On failure: update job status to `failed`, persist stderr to `download_job.error_log`

## Output

gallery-dl writes files to `DOWNLOAD_ROOT/{source}/{creator}/{work_id}/` using per-work directories. The import job then:
- Scans `DOWNLOAD_ROOT/{source}/` recursively for `.json` metadata files written by `--write-metadata`
- Groups JSONs by `source_work_id` (extracted via `provider.parse_work_source()`)
- Creates DB records (Work, WorkSource, Asset, AssetSource, Tags)
- Generates thumbnails (pyvips WebP) in `LIBRARY_ROOT/{source}/{creator}/{work_id}/`
- Writes `metadata.json` to `LIBRARY_ROOT/{source}/{creator}/{work_id}/`
- Deletes processed JSON files (image files stay in DOWNLOAD_ROOT)
- Original images remain in DOWNLOAD_ROOT for serving; LIBRARY_ROOT contains only metadata + thumbnails

## Timeout and retry

- Default timeout: 600 seconds per job
- Max retries: 3 per job
- Retry with exponential backoff (60s, 300s, 900s)
- Jobs stuck in `running` state for > 3600s are marked `stale` and can be manually retried

## Download job state machine

States (non-negotiable — this is the atomicity fix for the DOWNLOAD_ROOT → LIBRARY_ROOT gap):

```
pending
  → downloading    (worker picked up the job, started gallery-dl)
  → downloaded     (gallery-dl completed, files in DOWNLOAD_ROOT/<job_id>/)
  → importing      (import job in progress, DB records being created)
  → complete       (files moved to LIBRARY_ROOT, DB records committed)
  → failed         (gallery-dl or import failed, error_log populated)
  → stale          (no progress for > 3600s, requires manual intervention)
```

Transitions:
- `pending → downloading`: worker dequeues job
- `downloading → downloaded`: gallery-dl exits 0, files present
- `downloading → failed`: gallery-dl exits non-zero, stderr captured
- `downloaded → importing`: import job created and started
- `downloaded → failed`: import step crashed irrecoverably
- `importing → complete`: all files moved, all DB records committed
- `importing → failed`: import error, files may be partially moved
- Any state → `stale`: no state transition for 3600s (worker crash detection)

Stale jobs: the scheduler or admin can re-enqueue them to `pending`.

## Worker startup orphan reconciliation

On worker startup, before processing any new jobs:

1. Query `download_job` for jobs with status `downloading` or `importing`
2. Mark them `stale` (worker died mid-job)
3. Scan `DOWNLOAD_ROOT/<job_id>/` directories
4. For any directory without a corresponding `downloaded` or `importing` job:
   - Create a `download_job` with status `downloaded` (orphaned download)
   - Enqueue an import job

This ensures no downloaded files are silently lost on worker restart.

## Auth health tracking

On `subscription_source`:
- `last_successful_auth`: timestamp of last download that didn't fail with auth error
- After any download failure: check stderr for auth-related keywords (`401`, `403`, "login", "authentication", "forbidden")
- If auth failure detected: mark `auth_healthy = False`
- On next successful download: mark `auth_healthy = True`, update `last_successful_auth`
- Admin cookie/config status page queries `auth_healthy` per subscription source
