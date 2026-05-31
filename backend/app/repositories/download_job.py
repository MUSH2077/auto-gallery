from uuid import UUID

from sqlalchemy import select, delete as sql_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DownloadJob, ImportJob


class DownloadJobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, status: str | None = None, source: str | None = None,
                       subscription_id: str | None = None,
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
        return job

    async def update_status(self, job: DownloadJob, status: str, error_log: str | None = None) -> DownloadJob:
        job.status = status
        if error_log:
            job.error_log = error_log
        await self.session.flush()
        return job

    async def delete_by_status(self, statuses: list[str]) -> int:
        result = await self.session.execute(
            sql_delete(DownloadJob).where(DownloadJob.status.in_(statuses))
        )
        await self.session.flush()
        return result.rowcount

    async def kill_stuck(self) -> int:
        """Mark all 'downloading' jobs as 'stale' so they can be retried."""
        result = await self.session.execute(
            select(DownloadJob).where(DownloadJob.status == "downloading")
        )
        jobs = result.scalars().all()
        for j in jobs:
            j.status = "stale"
            if j.error_log:
                j.error_log += "\n[manual] Force-stale by user"
            else:
                j.error_log = "[manual] Force-stale by user"
        await self.session.flush()
        return len(jobs)

    async def list_imports(self, download_job_id: UUID) -> list[ImportJob]:
        result = await self.session.execute(
            select(ImportJob).where(ImportJob.download_job_id == download_job_id)
        )
        return list(result.scalars().all())

    async def create_import(self, data: dict) -> ImportJob:
        job = ImportJob(**data)
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_import(self, job_id: UUID) -> ImportJob | None:
        return await self.session.get(ImportJob, job_id)