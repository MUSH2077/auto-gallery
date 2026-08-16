from uuid import UUID

from sqlalchemy import select, delete as sql_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DownloadJob, ImportJob
from app.services.job_manifest import append_manifest_event
from app.models.task_state import transition_download_job
from app.services.search_projection_outbox import request_search_projection


class DownloadJobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, status: str | None = None, source: str | None = None,
                       subscription_id: str | None = None,
                       subscription_source_id: str | None = None,
                       sort_by: str = "created_at", sort_order: str = "desc",
                       offset: int = 0, limit: int = 50) -> list[DownloadJob]:
        order_col = getattr(DownloadJob, sort_by, DownloadJob.created_at)
        stmt = select(DownloadJob).offset(offset).limit(limit)
        if sort_order == "asc":
            stmt = stmt.order_by(order_col.asc())
        else:
            stmt = stmt.order_by(order_col.desc())
        if status:
            stmt = stmt.where(DownloadJob.status == status)
        if source:
            stmt = stmt.where(DownloadJob.source == source)
        if subscription_id:
            stmt = stmt.where(DownloadJob.subscription_id == subscription_id)
        if subscription_source_id:
            stmt = stmt.where(DownloadJob.subscription_source_id == subscription_source_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_status(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(DownloadJob)
        if status:
            stmt = stmt.where(DownloadJob.status == status)
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def get(self, job_id: UUID) -> DownloadJob | None:
        return await self.session.get(DownloadJob, job_id)

    async def create(self, data: dict) -> DownloadJob:
        job = DownloadJob(**data)
        self.session.add(job)
        await self.session.flush()
        if job.subscription_id:
            await request_search_projection(
                self.session,
                subscription_ids=[job.subscription_id],
            )
        return job

    async def update_status(self, job: DownloadJob, status: str, error_log: str | None = None) -> DownloadJob:
        old_status = job.status
        transition_download_job(job, status)
        # A successful terminal transition must be able to clear a previous
        # failure from the same retryable job record.
        job.error_log = error_log
        append_manifest_event(job, "status_changed", from_status=old_status, to_status=status, error_log=error_log)
        try:
            from app.services.tasks import TaskService
            task_svc = TaskService(self.session)
            await task_svc.ensure_download_task(job)
        except Exception:
            pass
        await self.session.flush()
        if job.subscription_id:
            await request_search_projection(
                self.session,
                subscription_ids=[job.subscription_id],
            )
        return job

    async def delete_by_status(self, statuses: list[str]) -> int:
        subscription_ids = set((await self.session.execute(
            select(DownloadJob.subscription_id)
            .where(DownloadJob.status.in_(statuses))
            .distinct()
        )).scalars().all())
        result = await self.session.execute(
            sql_delete(DownloadJob).where(DownloadJob.status.in_(statuses))
        )
        await request_search_projection(
            self.session,
            subscription_ids=subscription_ids,
        )
        await self.session.flush()
        return result.rowcount

    async def list_imports(self, download_job_id: UUID) -> list[ImportJob]:
        result = await self.session.execute(
            select(ImportJob).where(ImportJob.download_job_id == download_job_id)
        )
        return list(result.scalars().all())

    async def create_import(self, data: dict) -> ImportJob:
        job = ImportJob(**data)
        self.session.add(job)
        await self.session.flush()
        try:
            from app.services.tasks import TaskService
            await TaskService(self.session).ensure_import_task(job)
        except Exception:
            pass
        return job

    async def get_import(self, job_id: UUID) -> ImportJob | None:
        return await self.session.get(ImportJob, job_id)
