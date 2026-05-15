from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DownloadJob, ImportJob


class DownloadJobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, status: str | None = None, offset: int = 0, limit: int = 50) -> list[DownloadJob]:
        stmt = select(DownloadJob).offset(offset).limit(limit).order_by(DownloadJob.created_at.desc())
        if status:
            stmt = stmt.where(DownloadJob.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

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
