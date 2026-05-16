from uuid import UUID

from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.subscription import SubscriptionRepository
from app.models import SubscriptionSource, DownloadJob, ImportJob
from app.providers import registry


class SubscriptionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = SubscriptionRepository(db)

    async def list_subscriptions(self, offset: int = 0, limit: int = 50):
        return await self.repo.list_all(offset, limit)

    async def get_subscription(self, sub_id: UUID):
        sub = await self.repo.get(sub_id)
        if not sub:
            raise ValueError("Subscription not found")
        return sub

    async def create_subscription(self, data: dict):
        sub = await self.repo.create(data)
        await self.db.commit()
        return sub

    async def update_subscription(self, sub_id: UUID, data: dict):
        sub = await self.get_subscription(sub_id)
        sub = await self.repo.update(sub, data)
        await self.db.commit()
        return sub

    async def delete_subscription(self, sub_id: UUID):
        sub = await self.get_subscription(sub_id)

        # 1. Delete download jobs and their import jobs first
        djs = await self.db.execute(
            select(DownloadJob).where(DownloadJob.subscription_id == sub_id)
        )
        for dj in djs.scalars().all():
            await self.db.execute(
                sql_delete(ImportJob).where(ImportJob.download_job_id == dj.id)
            )
            await self.db.delete(dj)

        # 2. Delete subscription sources (no more FK references from download_jobs)
        await self.db.execute(
            sql_delete(SubscriptionSource).where(SubscriptionSource.subscription_id == sub_id)
        )

        # 3. Delete the subscription
        await self.db.delete(sub)
        await self.db.commit()

    async def add_source(self, data: dict):
        source = data.get("source", "")
        provider = registry.get(source)
        url = data.get("source_url", "")
        if url and not provider.validate_url(url):
            raise ValueError(f"Invalid URL for source '{source}': {url}")
        ss = await self.repo.add_source(data)
        await self.db.commit()
        return ss

    async def list_sources(self, subscription_id: UUID):
        return await self.repo.list_sources(subscription_id)

    async def update_source(self, ss_id: UUID, data: dict):
        ss = await self.repo.get_source(ss_id)
        if not ss:
            raise ValueError("SubscriptionSource not found")
        ss = await self.repo.update_source(ss, data)
        await self.db.commit()
        return ss

    async def delete_source(self, ss_id: UUID):
        ss = await self.repo.get_source(ss_id)
        if not ss:
            raise ValueError("SubscriptionSource not found")
        await self.repo.delete_source(ss)
        await self.db.commit()
