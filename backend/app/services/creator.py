from uuid import UUID

from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.creator import CreatorRepository
from app.models import Creator, CreatorLink, SourceCreator, Subscription, SubscriptionSource, DownloadJob, ImportJob
from app.schemas.creator import CreatorRead


class CreatorService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CreatorRepository(db)

    async def list_creators(self, offset: int = 0, limit: int = 50,
                            search: str | None = None,
                            is_active: bool | None = None,
                            has_danbooru: bool | None = None,
                            has_subscription: bool | None = None,
                            is_favorite: bool | None = None):
        creators, total = await self.repo.list_all(offset, limit,
                                        search=search, is_active=is_active,
                                        has_danbooru=has_danbooru,
                                        has_subscription=has_subscription,
                                        is_favorite=is_favorite)
        items = [CreatorRead.model_validate(c, from_attributes=True) for c in creators]
        return {"items": items, "total": total}

    async def toggle_favorite(self, creator_id: UUID):
        creator = await self.get_creator(creator_id)
        creator.is_favorite = not creator.is_favorite
        await self.db.commit()
        await self.db.refresh(creator)
        return creator

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
        await self.db.refresh(creator)
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
        await self.db.refresh(link)
        return link

    async def list_source_creators(self, creator_id: UUID):
        return await self.repo.list_source_creators(creator_id)

    async def delete_link(self, link_id: UUID):
        link = await self.repo.get_link(link_id)
        if not link:
            raise ValueError("CreatorLink not found")
        await self.repo.delete_link(link)
        await self.db.commit()

    async def enrich_from_danbooru(self, creator_id: UUID, source_url: str | None = None,
                                    pixiv_id: str | None = None,
                                    artist_name: str | None = None) -> int:
        """Query Danbooru and create CreatorLink suggestions for this creator."""
        import logging
        import asyncio
        logger = logging.getLogger(__name__)

        try:
            from app.services import danbooru as danbooru_svc

            # Danbooru HTTP is synchronous (urllib); run in thread to avoid
            # blocking the FastAPI event loop during API calls.
            artist, links = await asyncio.to_thread(
                danbooru_svc.search_and_extract,
                source_url=source_url, pixiv_id=pixiv_id, artist_name=artist_name,
            )
            if not links:
                return 0

            # Enrich creator record with Danbooru data
            creator = await self.repo.get(creator_id)
            if creator and artist:
                if creator.danbooru_artist_id is None:
                    creator.danbooru_artist_id = artist.get("id")
                if not creator.description:
                    parts = []
                    other_names = artist.get("other_names", [])
                    if other_names:
                        parts.append(f"Danbooru aliases: {', '.join(other_names)}")
                    notes = artist.get("notes")
                    if notes:
                        parts.append(notes)
                    if parts:
                        creator.description = "\n".join(parts)
                await self.db.flush()

            created = 0
            for link_data in links:
                existing = await self.db.execute(
                    select(CreatorLink).where(
                        CreatorLink.creator_id == creator_id,
                        CreatorLink.url == link_data["url"],
                    )
                )
                if existing.scalar_one_or_none():
                    continue
                self.db.add(CreatorLink(
                    creator_id=creator_id,
                    url=link_data["url"],
                    link_type=link_data["link_type"],
                    source=link_data["source"],
                    confidence=link_data["confidence"],
                    is_verified=link_data["is_verified"],
                    notes=link_data.get("notes"),
                ))
                created += 1

            if created:
                await self.db.commit()
            return created
        except Exception as e:
            from app.services.danbooru import DanbooruUnavailableError
            if isinstance(e, DanbooruUnavailableError):
                raise  # let the API layer report 503 instead of a silent zero
            logger.warning("Danbooru enrichment failed: %s", e)
            return 0
