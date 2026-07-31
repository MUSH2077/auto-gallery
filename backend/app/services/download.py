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
from app.services.job_progress import apply_download_progress
from app.services.progress import ProgressTracker
from app.services.redis_client import get_redis
from app.services.search import SearchService
from app.services.search_language import compose_search_query

logger = logging.getLogger(__name__)

RQ_JOB_TIMEOUT = 7200  # 2 hours — must exceed gallery-dl subprocess timeout
ACTIVE_DOWNLOAD_STATUSES = {"pending", "enqueued", "downloading", "downloaded", "importing"}


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

    def _enrich_progress(self, jobs):
        for job in jobs:
            if getattr(job, "status", None) not in ACTIVE_DOWNLOAD_STATUSES:
                continue
            progress = ProgressTracker.get(str(job.id))
            if progress:
                job.progress_data = progress
        return jobs

    async def list_jobs(self, status: str | None = None, source: str | None = None,
                        subscription_id: str | None = None,
                        subscription_source_id: str | None = None,
                        q: str | None = None,
                        sort_by: str = "created_at", sort_order: str = "desc",
                        offset: int = 0, limit: int = 50):
        canonical = q or ""
        for key, value in (
            ("status", status),
            ("source", source),
            ("repo", subscription_source_id),
        ):
            if value:
                canonical = compose_search_query(
                    canonical,
                    "tasks",
                    key=key,
                    value=value,
                    operation="add",
                ).canonical
        if sort_by in {"created_at", "updated_at"}:
            sort_value = f"{'updated' if sort_by == 'updated_at' else 'created'}-{'asc' if sort_order == 'asc' else 'desc'}"
            canonical = compose_search_query(
                canonical,
                "tasks",
                key="sort",
                value=sort_value,
                operation="set",
            ).canonical
        jobs = await SearchService(self.db).search_download_jobs(
            canonical,
            offset=offset,
            limit=limit,
        )
        # Kept only for legacy API callers. New UI deep-links use ``repo:``.
        if subscription_id:
            jobs = [job for job in jobs if str(job.subscription_id) == str(subscription_id)]
        jobs = await self._enrich_job_context(jobs)
        self._enrich_progress(jobs)
        return jobs

    async def get_job(self, job_id: UUID):
        job = await self.repo.get(job_id)
        if not job:
            raise ValueError("DownloadJob not found")
        enriched = await self._enrich_job_context([job])
        self._enrich_progress(enriched)
        return enriched[0]

    async def create_job(self, data: dict) -> dict:
        from app.services.download_orchestrator import DownloadOrchestrator
        return await DownloadOrchestrator(self.db).create(data, self.repo)

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
