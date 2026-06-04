import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.subscription_source import SubscriptionSource
from app.repositories.download_job import DownloadJobRepository
from app.services.settings import build_effective_gallerydl_config, get_download_defaults
from app.services.subscription_enqueue import mark_source_sync_success

logger = logging.getLogger(__name__)

FALLBACK_TIMEOUT = 600
FALLBACK_MAX_RETRIES = 3
FALLBACK_BACKOFF_BASE = 60


def _parse_progress(stderr: str) -> dict | None:
    """Extract download progress info from gallery-dl stderr."""
    import re as _re
    info = {}
    # Match patterns like "[1/50]" or "Downloading 5/10"
    m = _re.search(r'\[(\d+)/(\d+)\]', stderr)
    if m:
        info["current"] = int(m.group(1))
        info["total"] = int(m.group(2))
    # Match "x images" or "x files"
    m = _re.search(r'(\d+)\s*(?:image|file)s?\s*(?:downloaded|found)', stderr, _re.IGNORECASE)
    if m:
        info["downloaded"] = int(m.group(1))
    return info if info else None

# RQ job timeout — must exceed the longest gallery-dl subprocess timeout,
# otherwise RQ kills the function before subprocess.run finishes.
RQ_JOB_TIMEOUT = 7200  # 2 hours


async def _read_download_defaults():
    """Read download job defaults from system_settings table."""
    try:
        async with async_session() as db:
            return await get_download_defaults(db)
    except Exception:
        logger.warning("Failed to read download_defaults, using fallbacks", exc_info=True)
    return {}


AUTH_ERROR_PATTERNS = [
    (r"(?i)401\s*(unauthorized|error)", "HTTP 401 Unauthorized"),
    (r"(?i)403\s*(forbidden|error)", "HTTP 403 Forbidden"),
    (r"(?i)authentication\s*(required|failed|error)", "Authentication required"),
    (r"(?i)cookie.*(expired|invalid|missing)", "Cookie expired or missing"),
    (r"(?i)token.*(expired|invalid|revoked)", "Token expired or invalid"),
    (r"(?i)login.*(required|failed|error)", "Login required"),
    (r"(?i)no.*valid.*(cookie|session|token|auth)", "No valid credentials"),
]


def _cleanup_temp_config(path: str | None):
    """Remove temp config file."""
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass


def _snapshot_metadata_jsons(source: str) -> set[str]:
    """Return a set of absolute paths to all metadata JSON files for a source.

    Used to detect which files were created by THIS gallery-dl invocation
    via before/after diff — works regardless of naming template configuration.
    """
    source_root = Path(settings.download_root) / source
    if not source_root.exists():
        return set()
    return {str(p) for p in source_root.rglob("*.json") if p.is_file()}


def _count_new_artifacts(source: str, json_before: set[str]) -> tuple[int, int]:
    """Count NEW metadata JSONs and image files created since snapshot.

    Compares current filesystem against json_before to detect only files
    produced by the current gallery-dl run. Image count is scoped to
    directories containing new JSONs, avoiding files from other creators.
    """
    source_root = Path(settings.download_root) / source
    if not source_root.exists():
        return (0, 0)

    IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    json_after = {str(p) for p in source_root.rglob("*.json") if p.is_file()}
    new_jsons = json_after - json_before

    if not new_jsons:
        return (0, 0, set())

    # Count images only in directories that contain new JSONs
    # (avoids counting files from other creators' past downloads)
    new_dirs = {str(Path(jp).parent) for jp in new_jsons}
    img_count = 0
    for d in new_dirs:
        dir_path = Path(d)
        if not dir_path.exists():
            continue
        for p in dir_path.iterdir():
            if p.is_file() and p.suffix.lower() in IMG_EXTS:
                img_count += 1

    return (len(new_jsons), img_count, new_jsons)


AUTH_WARNING_PATTERNS = [
    (r"(?i)no.*PHPSESSID|no.*cookie.*set", "No auth cookie set (R-18 content may be missed)"),
    (r"(?i)warning.*auth|warning.*login|warning.*cookie|warning.*token", "Auth warning in output"),
    (r"(?i)user.*not found|user.*does not exist|user.*left", "User does not exist or has left"),
    (r"(?i)no\s+(valid|working).*(credential|auth|login|session)", "No valid credentials"),
    (r"(?i)request.*failed|connection.*refused|connection.*error|timeout", "Connection error"),
]


async def _enqueue_import(download_job_id: str, import_error: str | None = None, new_json_paths: set[str] | None = None):
    """Create an import job and enqueue it. Returns import_job_id or None.

    If new_json_paths is provided, stores the file list in Redis so the
    import runner can process exactly those files without re-scanning.
    """
    try:
        import redis as redis_lib
        from rq import Queue

        async with async_session() as db:
            repo = DownloadJobRepository(db)
            extra = {"error_log": import_error} if import_error else {}
            import_job = await repo.create_import({
                "download_job_id": UUID(download_job_id),
                "status": "pending",
                **extra,
            })
            await db.commit()
            import_job_id = str(import_job.id)

        # Store new JSON file paths in Redis for the import runner
        if new_json_paths:
            try:
                r = redis_lib.from_url(settings.redis_url)
                r.setex(
                    f"import:{import_job_id}:files",
                    7200,  # 2h TTL
                    json.dumps(list(new_json_paths)),
                )
            except Exception:
                logger.warning("Failed to store import file list for %s", import_job_id, exc_info=True)

        r = redis_lib.from_url(settings.redis_url)
        Queue(connection=r).enqueue(
            "app.jobs.import_runner.run_import_job", import_job_id,
            job_timeout=RQ_JOB_TIMEOUT)
        logger.info("Enqueued import job %s (recovery) for download %s", import_job_id, download_job_id)
        return import_job_id
    except Exception as e:
        logger.error("Failed to enqueue import for download %s: %s", download_job_id, e)
        return None


async def run_download_job(job_id: str):
    job_uuid = UUID(job_id)

    async with async_session() as db:
        repo = DownloadJobRepository(db)
        job = await repo.get(job_uuid)
        if not job:
            return

        # Guard against duplicate execution or paused jobs
        if job.status in ("downloading", "downloaded", "complete"):
            logger.warning("Job %s already in status %s, skipping", job_id, job.status)
            return
        if job.status == "paused":
            logger.info("Job %s is paused, skipping", job_id)
            return

        await repo.update_status(job, "downloading")
        if job.subscription_source_id:
            ss = await db.get(SubscriptionSource, job.subscription_source_id)
            if ss:
                ss.last_attempted_at = datetime.now(timezone.utc)
        await db.commit()

    dl_defaults = await _read_download_defaults()
    dl_timeout = int(dl_defaults.get("timeout_seconds", FALLBACK_TIMEOUT))
    max_retries = int(dl_defaults.get("max_retries", FALLBACK_MAX_RETRIES))
    backoff_base = int(dl_defaults.get("retry_backoff_base_seconds", FALLBACK_BACKOFF_BASE))

    skip_ai = dl_defaults.get("skip_ai_generated", False)
    ai_config_path = None
    job_config_path = None
    source_url = job.source_url

    # Write per-job gallery-dl config with provider directory/filename settings
    try:
        from app.providers import registry as _reg
        from app.models.naming_template import NamingTemplate as _NamingTemplate
        _prov = _reg.get(job.source)
        # Load the active naming template for this source (if any)
        _naming_tpl = None
        try:
            async with async_session() as _nt_db:
                _nt_result = await _nt_db.execute(
                    select(_NamingTemplate).where(
                        _NamingTemplate.source == job.source,
                        _NamingTemplate.is_default == True,
                    ).limit(1)
                )
                _naming_tpl = _nt_result.scalar_one_or_none()
        except Exception:
            logger.debug("Failed to load naming template for source %s", job.source, exc_info=True)
        _provider_cfg = _prov.build_gallerydl_config(None, None)
        _cfg = build_effective_gallerydl_config(
            job.source,
            _provider_cfg,
            _naming_tpl.template if _naming_tpl else None,
        )
        if _cfg:
            job_config_path = os.path.join(
                os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"),
                "jobs", f"job-{job_id}.json")
            os.makedirs(os.path.dirname(job_config_path), exist_ok=True)
            with open(job_config_path, "w") as f:
                json.dump(_cfg, f)
            async with async_session() as _cfg_db:
                from app.repositories.download_job import DownloadJobRepository as _DJRepo
                _cfg_j = await _DJRepo(_cfg_db).get(job_uuid)
                if _cfg_j:
                    _cfg_j.gallerydl_config_path = job_config_path
                    await _cfg_db.commit()
        # Record creator dir so import_runner can scope its scan
        try:
            _creator_dir = _prov.get_creator_dir_from_url(job.source_url)
            if _creator_dir and not job.download_dir:
                async with async_session() as _dir_db:
                    from app.repositories.download_job import DownloadJobRepository as _DJRepo
                    _dir_j = await _DJRepo(_dir_db).get(job_uuid)
                    if _dir_j and not _dir_j.download_dir:
                        _dir_j.download_dir = _creator_dir
                        await _dir_db.commit()
        except Exception:
            logger.debug("Failed to record download_dir for job %s", job_id, exc_info=True)
    except Exception:
        logger.debug("Failed to write per-job config for %s", job_id, exc_info=True)

    # Per-source AI filtering
    if skip_ai:
        if job.source == "pixiv":
            # Pixiv: use gallery-dl extractor config "ai-type": 1 (non-AI only)
            ai_config_path = os.path.join(
                os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"),
                "jobs", f"ai-filter-{job_id}.json")
            os.makedirs(os.path.dirname(ai_config_path), exist_ok=True)
            try:
                with open(ai_config_path, "w") as f:
                    json.dump({"extractor": {"pixiv": {"ai-type": 1}}}, f)
            except Exception:
                logger.warning("Failed to write AI filter config for job %s", job_id, exc_info=True)
                ai_config_path = None

        elif job.source == "danbooru":
            # Danbooru: exclude ai_generated tag from search URL
            import urllib.parse as url_parse
            parsed = url_parse.urlparse(source_url)
            params = url_parse.parse_qs(parsed.query)
            tags = params.get("tags", [""])[0]
            if tags and "-ai_generated" not in tags:
                tags += "+-ai_generated"
                new_query = url_parse.urlencode({"tags": tags}, doseq=True)
                source_url = url_parse.urlunparse(parsed._replace(query=new_query))

    # Snapshot metadata JSONs before running gallery-dl to detect new files.
    # This works regardless of the naming template configuration.
    json_before: set[str] = _snapshot_metadata_jsons(job.source)

    result = None
    try:
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")

        cmd = ["gallery-dl", "--write-metadata"]
        if os.path.exists(config_path):
            cmd.extend(["--config", config_path])
        if job_config_path:
            cmd.extend(["--config", job_config_path])
        if ai_config_path:
            cmd.extend(["--config", ai_config_path])

        archive_path = os.path.join(
            str(settings.download_root), f"archive-{job.source}.sqlite3")
        cmd.extend(["--download-archive", archive_path])

        max_posts = dl_defaults.get("max_posts") or dl_defaults.get("sync_batch_size", 200)
        cmd.extend(["--range", f"1-{max_posts}"])

        cmd.extend([
            "--destination", str(settings.download_root),
            source_url,
        ])

        env = os.environ.copy()
        proxy_enabled = False
        try:
            from app.services.proxy import _load_proxy_config, get_proxy_env
            proxy_config = await _load_proxy_config()
            proxy_enabled = proxy_config.get("enabled", False)
            env.update(get_proxy_env(proxy_config))
        except Exception:
            logger.warning("Failed to apply proxy env for download job %s", job_id, exc_info=True)

        logger.info("Running gallery-dl: %s (timeout=%ds, proxy=%s)", " ".join(cmd), dl_timeout, proxy_enabled)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=dl_timeout, env=env)
        logger.info("gallery-dl exit=%d, stdout=%d bytes, stderr=%d bytes", result.returncode, len(result.stdout), len(result.stderr))
        if result.returncode != 0:
            logger.warning("gallery-dl stderr (last 500): %s", result.stderr[-500:] if result.stderr else "(none)")

    except subprocess.TimeoutExpired:
        logger.warning("gallery-dl timed out after %ds for job %s", dl_timeout, job_id)
        result = None  # result is None signals timeout to the handlers below

    except Exception as e:
        logger.error("Unexpected error in download job %s: %s", job_id, e, exc_info=True)
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                j.retry_count += 1
                error_text = str(e)[:10000]
                if j.retry_count < max_retries:
                    await repo2.update_status(j, "pending", f"unexpected error: {error_text}")
                else:
                    await repo2.update_status(j, "failed", f"unexpected error: {error_text}")
                await db2.commit()

        # Always try partial import recovery
        metadata_count, _, new_json_paths = _count_new_artifacts(job.source, json_before)
        if metadata_count > 0:
            logger.info("Partial recovery: found %d metadata JSONs after unexpected error for job %s", metadata_count, job_id)
            await _enqueue_import(str(job_uuid), f"partial import after unexpected error (found {metadata_count} metadata files)")
        _cleanup_temp_config(ai_config_path)
        _cleanup_temp_config(job_config_path)
        return

    # ── Cleanup temp configs ──
    _cleanup_temp_config(ai_config_path)
    _cleanup_temp_config(job_config_path)

    # ── Handle subprocess result ──

    async with async_session() as db2:
        repo2 = DownloadJobRepository(db2)
        j = await repo2.get(job_uuid)
        if not j:
            return

        if result is not None:
            # Normal completion (success or non-zero exit)
            if result.returncode == 0:
                await repo2.update_status(j, "downloaded")
            else:
                j.retry_count += 1
                if j.retry_count < max_retries:
                    await repo2.update_status(j, "pending", result.stderr[:5000] if result.stderr else None)
                else:
                    await repo2.update_status(j, "failed", result.stderr[:5000] if result.stderr else None)

            # Auth health monitoring — scan stderr regardless of exit code
            if j.subscription_source_id:
                ss = await db2.execute(
                    select(SubscriptionSource).where(SubscriptionSource.id == j.subscription_source_id)
                )
                source = ss.scalar_one_or_none()
                if source:
                    combined = (result.stderr or "") + (result.stdout or "")
                    auth_issue = None
                    for pattern, label in AUTH_ERROR_PATTERNS:
                        if re.search(pattern, combined):
                            auth_issue = label
                            break
                    if result is not None and result.returncode == 0 and not auth_issue:
                        source.last_successful_auth = datetime.now(timezone.utc)
                        source.auth_healthy = True
                    elif auth_issue:
                        source.auth_healthy = False
                        logger.warning("Auth issue for subscription_source %s: %s", source.id, auth_issue)
        else:
            # Timeout — subprocess.TimeoutExpired was caught
            j.retry_count += 1
            timeout_msg = f"timeout after {dl_timeout}s"
            if j.retry_count < max_retries:
                await repo2.update_status(j, "pending", timeout_msg)
            else:
                await repo2.update_status(j, "failed", timeout_msg)

        await db2.commit()

    # ── Enqueue import on success, auto-retry on failure, partial recovery on timeout ──

    if result is not None and result.returncode == 0:
        # Full success — but only enqueue import if there are new metadata JSONs.
        # gallery-dl exits 0 even when all files were skipped (already in archive),
        # or when the source has no content at all.
        metadata_count, image_count, new_json_paths = _count_new_artifacts(job.source, json_before)
        if metadata_count > 0:
            await _enqueue_import(str(job_uuid), new_json_paths=new_json_paths)
        else:
            # No new metadata JSONs — distinguish "already imported" from "nothing to download"
            # Check stderr for warnings that explain the zero-download result
            stderr_text = result.stderr or ""
            stdout_text = result.stdout or ""
            combined = stderr_text + stdout_text
            auth_warning = None
            for pattern, label in AUTH_WARNING_PATTERNS:
                if re.search(pattern, combined):
                    auth_warning = label
                    break

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

            async with async_session() as db3:
                repo3 = DownloadJobRepository(db3)
                j3 = await repo3.get(job_uuid)
                if j3:
                    await repo3.update_status(j3, new_status, new_error)
                    if new_status == "complete" and j3.subscription_source_id:
                        await mark_source_sync_success(db3, j3.subscription_source_id)
                    await db3.commit()

    elif result is not None and result.returncode != 0:
        # Non-zero exit — maybe partial files were downloaded
        metadata_count, _, new_json_paths = _count_new_artifacts(job.source, json_before)
        if metadata_count > 0:
            logger.info("Partial recovery: found %d metadata JSONs after failure for job %s", metadata_count, job_id)
            await _enqueue_import(str(job_uuid), f"partial import after download failure (found {metadata_count} metadata files)")

    else:
        # Timeout — attempt partial import recovery
        metadata_count, _, new_json_paths = _count_new_artifacts(job.source, json_before)
        if metadata_count > 0:
            logger.info("Partial recovery: found %d metadata JSONs after timeout for job %s", metadata_count, job_id)
            await _enqueue_import(str(job_uuid), f"partial import after timeout (found {metadata_count} metadata files)")

    # ── Enqueue retry for transient failures ──

    if j and j.retry_count < max_retries:
        # Only auto-retry non-zero exits and timeouts (skip unexpected errors — already handled above)
        needs_retry = (result is None) or (result.returncode != 0)
        if needs_retry:
            try:
                import redis as redis_lib
                from rq import Queue
                r = redis_lib.from_url(settings.redis_url)
                Queue(connection=r).enqueue_in(
                    timedelta(seconds=backoff_base * (2 ** (j.retry_count - 1))),
                    "app.jobs.download.run_download_job", job_id,
                    job_timeout=RQ_JOB_TIMEOUT)
                logger.info("Enqueued retry %d/%d for job %s in %ds",
                           j.retry_count, max_retries, job_id,
                           backoff_base * (2 ** (j.retry_count - 1)))
            except Exception:
                logger.warning("Failed to enqueue retry for download job %s", job_id, exc_info=True)
