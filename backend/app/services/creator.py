from uuid import UUID

from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.creator import CreatorRepository
from app.models import Creator, CreatorLink, SourceCreator, Subscription, SubscriptionSource, DownloadJob, ImportJob


class CreatorService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CreatorRepository(db)

    async def list_creators(self, offset: int = 0, limit: int = 50):
        return await self.repo.list_all(offset, limit)

    async def get_creator(self, creator_id: UUID):
        creator = await self.repo.get(creator_id)
        if not creator:
            raise ValueError("Creator not found")
        return creator

    async def create_creator(self, data: dict):
        creator = await self.repo.create(data)
        await self.db.commit()
        return creator

    async def update_creator(self, creator_id: UUID, data: dict):
        creator = await self.get_creator(creator_id)
        creator = await self.repo.update(creator, data)
        await self.db.commit()
        return creator

    async def delete_creator(self, creator_id: UUID):
        creator = await self.get_creator(creator_id)

        # 1. Find all subscriptions for this creator
        subs = await self.db.execute(
            select(Subscription).where(Subscription.creator_id == creator_id)
        )
        subscriptions = subs.scalars().all()

        for sub in subscriptions:
            # Delete download_jobs and their import_jobs first (FK to subscription_sources)
            djs = await self.db.execute(
                select(DownloadJob).where(DownloadJob.subscription_id == sub.id)
            )
            for dj in djs.scalars().all():
                await self.db.execute(
                    sql_delete(ImportJob).where(ImportJob.download_job_id == dj.id)
                )
                await self.db.delete(dj)
            # Delete subscription_sources (no more FK from download_jobs)
            await self.db.execute(
                sql_delete(SubscriptionSource).where(SubscriptionSource.subscription_id == sub.id)
            )
            await self.db.delete(sub)

        # 2. Set source_creators.creator_id to NULL (nullable column)
        await self.db.execute(
            sql_delete(SourceCreator).where(SourceCreator.creator_id == creator_id)
        )

        # 3. Delete creator links
        await self.db.execute(
            sql_delete(CreatorLink).where(CreatorLink.creator_id == creator_id)
        )

        # 4. Delete the creator
        await self.db.delete(creator)
        await self.db.commit()

    async def add_source_creator(self, data: dict):
        sc = await self.repo.add_source_creator(data)
        await self.db.commit()
        return sc

    async def list_links(self, creator_id: UUID):
        return await self.repo.list_links(creator_id)

    async def add_link(self, data: dict):
        link = await self.repo.add_link(data)
        await self.db.commit()
        return link

    async def update_link(self, link_id: UUID, data: dict):
        link = await self.repo.get_link(link_id)
        if not link:
            raise ValueError("CreatorLink not found")
        link = await self.repo.update_link(link, data)
        await self.db.commit()
        return link

    async def list_source_creators(self, creator_id: UUID):
        return await self.repo.list_source_creators(creator_id)

    async def delete_link(self, link_id: UUID):
        link = await self.repo.get_link(link_id)
        if not link:
            raise ValueError("CreatorLink not found")
        await self.repo.delete_link(link)
        await self.db.commit()
