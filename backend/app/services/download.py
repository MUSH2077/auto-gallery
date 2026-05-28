import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.repositories.download_job import DownloadJobRepository
from app.repositories.subscription import SubscriptionRepository
from app.providers import registry

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200  # 2 hours — must exceed gallery-dl subprocess timeout


class DownloadService:
    def __init__(self, db: AsyncSession):
        self.repo = DownloadJobRepository(db)
        self.sub_repo = SubscriptionRepository(db)
        self.db = db

    async def list_jobs(self, status: str | None = None, offset: int = 0, limit: int = 50):
        return await self.repo.list_all(status, offset, limit)

    async def get_job(self, job_id: UUID):
        job = await self.repo.get(job_id)
        if not job:
            raise ValueError("DownloadJob not found")
        return job

    async def create_job(self, data: dict) -> dict:
        source = data.get("source", "")
        source_url = data.get("source_url", "")
        subscription_source_id = data.get("subscription_source_id")

        # If subscription_source_id is provided, look up its source_url
        ss_uuid = subscription_source_id if isinstance(subscription_source_id, UUID) else UUID(subscription_source_id) if subscription_source_id else None
        if ss_uuid and not source_url:
            ss = await self.sub_repo.get_source(ss_uuid)
            if ss:
                source = ss.source
                source_url = ss.source_url or ""
                data["source"] = source

        if not source_url:
            raise ValueError("source_url is required")

        provider = registry.get(source)
        if not provider.validate_url(source_url):
            raise ValueError(f"Invalid URL for source '{source}': {source_url}")

        normalized_url = provider.normalize_url(source_url) or source_url

        job = await self.repo.create({
            "subscription_id": data["subscription_id"],
            "subscription_source_id": subscription_source_id,
            "source": source,
            "source_url": normalized_url,
            "status": "pending",
        })
        await self.db.commit()

        try:
            import redis as redis_lib
            from rq import Queue
            r = redis_lib.from_url(settings.redis_url)
            q = Queue(connection=r)
            q.enqueue("app.jobs.download.run_download_job", str(job.id), job_timeout=RQ_JOB_TIMEOUT)
        except Exception:
            logger.warning("Failed to enqueue download job %s", job.id, exc_info=True)

        return {"job_id": str(job.id), "status": job.status, "source_url": normalized_url}

    async def retry_job(self, job_id: UUID):
        job = await self.get_job(job_id)
        if job.status not in ("failed", "stale", "downloading"):
            raise ValueError(f"Cannot retry job with status '{job.status}'")
        job = await self.repo.update_status(job, "pending")
        try:
            import redis as redis_lib
            from rq import Queue
            r = redis_lib.from_url(settings.redis_url)
            Queue(connection=r).enqueue("app.jobs.download.run_download_job", str(job.id), job_timeout=RQ_JOB_TIMEOUT)
        except Exception:
            logger.warning("Failed to enqueue retry for download job %s", job.id, exc_info=True)
        return {"job_id": str(job.id), "status": job.status}

    async def delete_job(self, job_id: UUID):
        job = await self.get_job(job_id)
        if job.status in ("pending", "importing"):
            raise ValueError(f"Cannot delete job with status '{job.status}'")
        await self.db.delete(job)
        await self.db.commit()

    async def pause_job(self, job_id: UUID):
        job = await self.get_job(job_id)
        if job.status not in ("pending", "downloading"):
            raise ValueError(f"Cannot pause job with status '{job.status}'")
        job = await self.repo.update_status(job, "paused")
        return {"job_id": str(job.id), "status": job.status}

    async def resume_job(self, job_id: UUID):
        job = await self.get_job(job_id)
        if job.status not in ("paused",):
            raise ValueError(f"Cannot resume job with status '{job.status}'")
        job = await self.repo.update_status(job, "pending")
        try:
            import redis as redis_lib
            from rq import Queue
            r = redis_lib.from_url(settings.redis_url)
            Queue(connection=r).enqueue("app.jobs.download.run_download_job", str(job.id), job_timeout=RQ_JOB_TIMEOUT)
        except Exception:
            logger.warning("Failed to enqueue resume for download job %s", job.id, exc_info=True)
        return {"job_id": str(job.id), "status": job.status}

    async def batch_action(self, ids: list[UUID], action: str) -> dict:
        results = {"succeeded": 0, "failed": 0, "errors": []}
        for jid in ids:
            try:
                if action == "retry":
                    await self.retry_job(jid)
                elif action == "delete":
                    await self.delete_job(jid)
                elif action == "pause":
                    await self.pause_job(jid)
                elif action == "resume":
                    await self.resume_job(jid)
                else:
                    raise ValueError(f"Unknown action: {action}")
                results["succeeded"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"id": str(jid), "error": str(e)})
        return results

    async def list_imports(self, job_id: UUID):
        return await self.repo.list_imports(job_id)
