import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, select

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

    async def _enrich_job_context(self, jobs, *, user_id: int | None = None):
        if not jobs:
            return jobs
        sub_ids = {job.subscription_id for job in jobs if job.subscription_id}
        if not sub_ids:
            return jobs

        from app.models.remote_discovery import UserSubscription

        columns = [
                Subscription.id,
                Subscription.name,
                Subscription.creator_id,
                Creator.display_name,
                Creator.name,
        ]
        if user_id is not None:
            columns.append(UserSubscription.name)
        stmt = (
            select(*columns)
            .join(Creator, Creator.id == Subscription.creator_id)
            .where(Subscription.id.in_(sub_ids))
        )
        if user_id is not None:
            stmt = stmt.join(
                UserSubscription,
                and_(
                    UserSubscription.subscription_id == Subscription.id,
                    UserSubscription.user_id == user_id,
                ),
            )
        result = await self.db.execute(stmt)
        context = {
            sub_id: {
                "subscription_name": row[5] if user_id is not None else sub_name,
                "creator_id": creator_id,
                "creator_name": creator_display_name or creator_name,
            }
            for row in result.all()
            for sub_id, sub_name, creator_id, creator_display_name, creator_name in [row[:5]]
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
                from sqlalchemy.orm.attributes import set_committed_value
                set_committed_value(job, "progress_data", progress)
        return jobs

    async def list_jobs(self, status: str | None = None, source: str | None = None,
                        subscription_id: str | None = None,
                        subscription_source_id: str | None = None,
                        q: str | None = None,
                        visibility: str = "all",
                        sort_by: str = "created_at", sort_order: str = "desc",
                        offset: int = 0, limit: int = 50,
                        user_id: int | None = None):
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
            visibility=visibility,
            user_id=user_id,
            subscription_id=subscription_id,
        )
        jobs = await self._enrich_job_context(jobs, user_id=user_id)
        from app.services.task_actions import enrich_actions
        await enrich_actions(self.db, jobs, user_id=user_id, domain_kind="download")
        self._enrich_progress(jobs)
        return jobs

    async def get_job(self, job_id: UUID, *, user_id: int | None = None):
        if user_id is None:
            job = await self.repo.get(job_id)
        else:
            from app.models.download_job import DownloadJob
            from app.services.tasks import download_job_visibility_condition

            job = (
                await self.db.execute(
                    select(DownloadJob).where(
                        DownloadJob.id == job_id,
                        download_job_visibility_condition(user_id),
                    )
                )
            ).scalar_one_or_none()
        if not job:
            raise ValueError("DownloadJob not found")
        enriched = await self._enrich_job_context([job], user_id=user_id)
        from app.services.task_actions import enrich_actions
        await enrich_actions(self.db, enriched, user_id=user_id, domain_kind="download")
        self._enrich_progress(enriched)
        return enriched[0]

    async def create_job(self, data: dict, *, user_id: int | None = None) -> dict:
        from app.services.download_orchestrator import DownloadOrchestrator
        return await DownloadOrchestrator(self.db).create(
            data,
            self.repo,
            user_id=user_id,
        )

    async def retry_job(self, job_id: UUID):
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.retry_download(job_id)

    async def delete_job(self, job_id: UUID):
        from app.services.task_engine import TaskEngine
        await TaskEngine(self.db).delete_download(job_id)

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

    async def clear_completed(self, statuses: list[str], *, user_id: int | None = None) -> dict:
        """Delete all jobs matching given statuses (e.g. complete, failed, stale)."""
        from app.services.task_bulk import owned_batch_ids
        from app.services.task_engine import TaskEngine
        if user_id is None:
            raise ValueError("History deletion requires an actor")
        ids = await owned_batch_ids(self.db, "download", {"statuses": statuses}, user_id)
        result = await TaskEngine(self.db).batch_by_filter("download", {"ids": [str(i) for i in ids]}, "delete")
        return result

    async def kill_stuck_jobs(self) -> int:
        """Detect stale tasks via heartbeat timeout."""
        from app.services.task_engine import TaskEngine
        engine = TaskEngine(self.db)
        return await engine.detect_stale_tasks()

    async def list_imports(self, job_id: UUID):
        return await self.repo.list_imports(job_id)
