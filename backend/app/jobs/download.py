import logging
import os
import subprocess
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.database import async_session
from app.repositories.download_job import DownloadJobRepository

logger = logging.getLogger(__name__)


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
        download_dir = Path(settings.download_root) / job_id
        download_dir.mkdir(parents=True, exist_ok=True)

        cmd = ["gallery-dl"]
        if os.path.exists(config_path):
            cmd.extend(["--config", config_path])
        cmd.extend([
            "--destination", str(download_dir),
            "--write-metadata",
            job.source_url,
        ])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                if result.returncode == 0:
                    await repo2.update_status(j, "downloaded")
                else:
                    j.retry_count += 1
                    if j.retry_count < 3:
                        await repo2.update_status(j, "pending", result.stderr[:5000])
                    else:
                        await repo2.update_status(j, "failed", result.stderr[:5000])
                await db2.commit()

        if result.returncode != 0 and job.retry_count < 3:
            try:
                import redis as redis_lib
                from rq import Queue
                r = redis_lib.from_url(settings.redis_url)
                Queue(connection=r).enqueue_in(
                    60 * (2 ** (job.retry_count - 1)),
                    "app.jobs.download.run_download_job", job_id)
            except Exception:
                pass

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
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                j.retry_count += 1
                await repo2.update_status(j, "failed", "timeout after 600s")
                await db2.commit()
    except Exception as e:
        async with async_session() as db2:
            repo2 = DownloadJobRepository(db2)
            j = await repo2.get(job_uuid)
            if j:
                await repo2.update_status(j, "failed", str(e)[:10000])
                await db2.commit()
