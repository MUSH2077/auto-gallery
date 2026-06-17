import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.creator import Creator
from app.models.subscription import Subscription
from app.repositories.download_job import DownloadJobRepository
from app.repositories.subscription import SubscriptionRepository
from app.providers import registry
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200  # 2 hours — must exceed gallery-dl subprocess timeout


class DownloadService:
    def __init__(self, db: AsyncSession):
        self.repo = DownloadJobRepository(db)
        self.sub_repo = SubscriptionRepository(db)
        self.db = db

    async def _enrich_job_context(self, jobs):
        if not jobs:
            return jobs
        sub_ids = {job.subscription_id for job in jobs if job.subscription_id}
        if not sub_ids:
            return jobs

        result = await self.db.execute(
            select(
                Subscription.id,
                Subscription.name,
                Subscription.creator_id,
                Creator.display_name,
                Creator.name,
            )
            .join(Creator, Creator.id == Subscription.creator_id)
            .where(Subscription.id.in_(sub_ids))
        )
        context = {
            sub_id: {
                "subscription_name": sub_name,
                "creator_id": creator_id,
                "creator_name": creator_display_name or creator_name,
            }
            for sub_id, sub_name, creator_id, creator_display_name, creator_name in result.all()
        }
        for job in jobs:
            item = context.get(job.subscription_id)
            if item:
                job.subscription_name = item["subscription_name"]
                job.creator_id = item["creator_id"]
                job.creator_name = item["creator_name"]
            else:
                job.subscription_name = None
                job.creator_id = None
                job.creator_name = None
        return jobs

    async def list_jobs(self, status: str | None = None, source: str | None = None,
                        subscription_id: str | None = None,
                        subscription_source_id: str | None = None,
                        q: str | None = None,
                        sort_by: str = "created_at", sort_order: str = "desc",
                        offset: int = 0, limit: int = 50):
        jobs = await self.repo.list_all(status=status, source=source,
                                        subscription_id=subscription_id,
                                        subscription_source_id=subscription_source_id,
                                        q=q,
                                        sort_by=sort_by, sort_order=sort_order,
                                        offset=offset, limit=limit)
        jobs = await self._enrich_job_context(jobs)
        if q:
            needle = q.strip().lower()
            jobs = [
                job for job in jobs
                if needle in str(job.id).lower()
                or needle in (job.source_url or "").lower()
                or needle in (getattr(job, "creator_name", None) or "").lower()
                or needle in (getattr(job, "subscription_name", None) or "").lower()
            ]
        return jobs

    async def get_job(self, job_id: UUID):
        job = await self.repo.get(job_id)
        if not job:
            raise ValueError("DownloadJob not found")
        enriched = await self._enrich_job_context([job])
        return enriched[0]

    async def create_job(self, data: dict) -> dict:
        source = data.get("source", "")
        source_url = data.get("source_url", "")
        subscription_source_id = data.get("subscription_source_id")

        # If subscription_source_id is provided, look up its source_url
        ss_uuid = subscription_source_id if isinstance(subscription_source_id, UUID) else UUID(subscription_source_id) if subscription_source_id else None
        if ss_uuid:
            from app.services.subscription_enqueue import enqueue_subscription_source_sync
            result = await enqueue_subscription_source_sync(self.db, ss_uuid, trigger="manual_source")
            if result["status"] == "enqueued":
                return {"job_id": result["job_id"], "status": "enqueued", "source_url": result["source_url"]}
            if result.get("job_id") and result.get("skip_reason") == "already_running":
                running = await self.repo.get(UUID(result["job_id"]))
                return {
                    "job_id": result["job_id"],
                    "status": running.status if running else "pending",
                    "source_url": running.source_url if running else source_url,
                }
            raise ValueError(result.get("skip_reason") or result.get("error") or "Unable to enqueue source sync")

        if not source_url:
            raise ValueError("source_url is required")

        try:
            provider = registry.get(source)
        except KeyError:
            raise ValueError(f"Unknown source provider: {source}")
        normalized_url = provider.normalize_url(source_url) or source_url
        if not provider.validate_url(normalized_url):
            raise ValueError(f"Invalid URL for source '{source}': {source_url}")

        job = await self.repo.create({
            "subscription_id": data["subscription_id"],
            "subscription_source_id": subscription_source_id,
            "source": source,
            "source_url": normalized_url,
            "status": "enqueued",
        })
        update_manifest(job, trigger="manual_url", source=source, source_url=normalized_url)
        append_manifest_event(job, "created", trigger="manual_url")
        await self.db.commit()

        try:
            from rq import Queue
            q = Queue(name="downloads", connection=get_redis())
            q.enqueue("app.jobs.download.run_download_job", str(job.id), job_timeout=RQ_JOB_TIMEOUT)
            append_manifest_event(job, "enqueued", queue="downloads")
            await self.db.commit()
        except Exception:
            logger.warning("Failed to enqueue download job %s", job.id, exc_info=True)

        return {"job_id": str(job.id), "status": job.status, "source_url": normalized_url}

    async def retry_job(self, job_id: UUID):
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.retry_download(job_id)

    async def delete_job(self, job_id: UUID):
        job = await self.get_job(job_id)
        if job.status in ("importing",):
            raise ValueError(f"Cannot delete job with status '{job.status}' — import is in progress")
        await self.db.delete(job)
        await self.db.commit()

    async def pause_job(self, job_id: UUID):
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.pause_download(job_id)

    async def resume_job(self, job_id: UUID):
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.resume_download(job_id)

    async def batch_action(self, ids: list[UUID], action: str) -> dict:
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.batch_by_filter("download", {"ids": [str(i) for i in ids]}, action)

    async def clear_completed(self, statuses: list[str]) -> int:
        """Delete all jobs matching given statuses (e.g. complete, failed, stale)."""
        count = await self.repo.delete_by_status(statuses)
        await self.db.commit()
        return count

    async def kill_stuck_jobs(self) -> int:
        """Detect stale tasks via heartbeat timeout."""
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.detect_stale_tasks()

    async def list_imports(self, job_id: UUID):
        return await self.repo.list_imports(job_id)
