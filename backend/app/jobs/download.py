import os, subprocess, asyncio
from pathlib import Path
from uuid import UUID
from app.config import settings
from app.database import async_session
from app.repositories.download_job import DownloadJobRepository

async def run_download_job(job_id: str):
    job_uuid = UUID(job_id)
    async with async_session() as db:
        repo = DownloadJobRepository(db)
        job = await repo.get(job_uuid)
        if not job: return

        await repo.update_status(job, "downloading")
        await db.commit()

        try:
            download_dir = Path(settings.download_root) / job_id
            download_dir.mkdir(parents=True, exist_ok=True)
            config_path = os.path.join(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config"), "config.json")
            cmd = ["gallery-dl"]
            if os.path.exists(config_path): cmd.extend(["--config", config_path])
            cmd.extend(["--destination", str(download_dir), "--write-metadata", "--write-info-json", job.source_url])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

            if result.returncode == 0:
                await repo.update_status(job, "downloaded")
                import_job = await repo.create_import({"download_job_id": job_uuid, "status": "pending"})
                await db.commit()
                try:
                    import redis as redis_lib; from rq import Queue
                    r = redis_lib.from_url(settings.redis_url)
                    Queue(connection=r).enqueue("app.jobs.import_runner.run_import_job", str(import_job.id))
                except: pass
            else:
                job.retry_count += 1
                err = result.stderr[:5000]
                if job.retry_count < 3:
                    await repo.update_status(job, "pending", err)
                    await db.commit()
                    # Re-enqueue with delay
                    try:
                        import redis as redis_lib; from rq import Queue
                        r = redis_lib.from_url(settings.redis_url)
                        Queue(connection=r).enqueue_in(60 * (2 ** (job.retry_count - 1)), "app.jobs.download.run_download_job", job_id)
                    except: pass
                else:
                    await repo.update_status(job, "failed", err)
                    await db.commit()
        except subprocess.TimeoutExpired:
            job.retry_count += 1
            await repo.update_status(job, "failed", "timeout after 600s")
            await db.commit()
        except Exception as e:
            await repo.update_status(job, "failed", str(e)[:10000])
            await db.commit()
