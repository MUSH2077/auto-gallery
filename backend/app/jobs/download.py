import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.subscription_source import SubscriptionSource
from app.repositories.download_job import DownloadJobRepository

logger = logging.getLogger(__name__)

FALLBACK_TIMEOUT = 600
FALLBACK_MAX_RETRIES = 3
FALLBACK_BACKOFF_BASE = 60


async def _read_download_defaults():
    """Read download job defaults from system_settings table."""
    try:
        from app.models.system_setting import SystemSetting
        async with async_session() as db:
            result = await db.execute(
                select(SystemSetting).where(SystemSetting.key == "download_defaults")
            )
            row = result.scalar_one_or_none()
            if row and row.value:
                return row.value
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


async def run_download_job(job_id: str):
    job_uuid = UUID(job_id)
    import_job_id = None

    async with async_session() as db:
        repo = DownloadJobRepository(db)
        job = await repo.get(job_uuid)
        if not job:
            return

        # Guard against duplicate execution
        if job.status in ("downloading", "downloaded", "complete"):
            logger.warning("Job %s already in status %s, skipping", job_id, job.status)
            return

        await repo.update_status(job, "downloading")
        import_job = await repo.create_import({"download_job_id": job_uuid, "status": "pending"})
        import_job_id = str(import_job.id)
        await db.commit()

    result = None
    try:
        config_path = os.path.join(
            os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")

        cmd = ["gallery-dl"]
        if os.path.exists(config_path):
            cmd.extend(["--config", config_path])

        # Archive: skip already-downloaded works per source
        archive_path = os.path.join(
            str(settings.download_root), f"archive-{job.source}.sqlite3")
        cmd.extend(["--download-archive", archive_path])

        # Limit batch size to avoid timeouts on large portfolios
        dl_defaults = await _read_download_defaults()
        max_posts = dl_defaults.get("max_posts") or dl_defaults.get("sync_batch_size", 200)
        cmd.extend(["--range", f"1-{max_posts}"])

        cmd.extend([
            "--destination", str(settings.download_root),
            job.source_url,
        ])

        dl_timeout = int(dl_defaults.get("timeout_seconds", FALLBACK_TIMEOUT))

        # Apply proxy env vars for gallery-dl subprocess
        env = os.environ.copy()
        try:
            from app.services.proxy import _load_proxy_config, get_proxy_env
            proxy_config = await _load_proxy_config()
            env.update(get_proxy_env(proxy_config))
        except Exception:
            logger.warning("Failed to apply proxy env for download job %s", job_id, exc_info=True)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=dl_timeout, env=env)

        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                if result.returncode == 0:
                    await repo2.update_status(j, "downloaded")
                else:
                    j.retry_count += 1
                    if j.retry_count < int(dl_defaults.get("max_retries", FALLBACK_MAX_RETRIES)):
                        await repo2.update_status(j, "pending", result.stderr[:5000])
                    else:
                        await repo2.update_status(j, "failed", result.stderr[:5000])
                # Auth health monitoring
                if j.subscription_source_id:
                    ss = await db2.execute(
                        select(SubscriptionSource).where(SubscriptionSource.id == j.subscription_source_id)
                    )
                    source = ss.scalar_one_or_none()
                    if source:
                        if result.returncode == 0:
                            source.last_successful_auth = datetime.now(timezone.utc)
                            source.auth_healthy = True
                        else:
                            combined = (result.stderr or "") + (result.stdout or "")
                            auth_issue = None
                            for pattern, label in AUTH_ERROR_PATTERNS:
                                if re.search(pattern, combined):
                                    auth_issue = label
                                    break
                            if auth_issue:
                                source.auth_healthy = False
                                logger.warning("Auth failure for subscription_source %s: %s", source.id, auth_issue)

                await db2.commit()

        max_retries = int(dl_defaults.get("max_retries", FALLBACK_MAX_RETRIES))
        backoff_base = int(dl_defaults.get("retry_backoff_base_seconds", FALLBACK_BACKOFF_BASE))

        if result.returncode != 0 and job.retry_count < max_retries:
            try:
                import redis as redis_lib
                from rq import Queue
                r = redis_lib.from_url(settings.redis_url)
                Queue(connection=r).enqueue_in(
                    backoff_base * (2 ** (job.retry_count - 1)),
                    "app.jobs.download.run_download_job", job_id)
            except Exception:
                logger.warning("Failed to enqueue retry for download job %s", job_id, exc_info=True)

        # Enqueue import after successful download
        if result.returncode == 0 and import_job_id:
            try:
                import redis as redis_lib
                from rq import Queue
                r = redis_lib.from_url(settings.redis_url)
                Queue(connection=r).enqueue(
                    "app.jobs.import_runner.run_import_job", import_job_id)
                logger.info("Enqueued import job %s after download", import_job_id)
            except Exception as e:
                logger.error("Failed to enqueue import job %s: %s", import_job_id, e)

    except subprocess.TimeoutExpired:
        dl_defaults_t = await _read_download_defaults()
        dl_timeout_t = int(dl_defaults_t.get("timeout_seconds", FALLBACK_TIMEOUT))
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                j.retry_count += 1
                await repo2.update_status(j, "failed", f"timeout after {dl_timeout_t}s")
                await db2.commit()
    except Exception as e:
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                await repo2.update_status(j, "failed", str(e)[:10000])
                await db2.commit()
