import logging
from uuid import UUID

from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.subscription import SubscriptionRepository
from app.models import SubscriptionSource, DownloadJob, ImportJob, CreatorLink, Subscription
from app.providers import registry
from app.services.settings import get_subscription_defaults

logger = logging.getLogger(__name__)

class SubscriptionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = SubscriptionRepository(db)

    async def list_subscriptions(self, offset: int = 0, limit: int = 50,
                                  search: str | None = None,
                                  is_active: bool | None = None,
                                  sync_enabled: bool | None = None,
                                  never_synced: bool | None = None):
        return await self.repo.list_all(offset, limit,
                                        search=search, is_active=is_active,
                                        sync_enabled=sync_enabled,
                                        never_synced=never_synced)

    async def get_subscription(self, sub_id: UUID):
        sub = await self.repo.get(sub_id)
        if not sub:
            raise ValueError("Subscription not found")
        return sub

    async def create_subscription(self, data: dict):
        # Merge system defaults: provided values take precedence over defaults
        defaults = await get_subscription_defaults(self.db)
        merged = {**defaults, **{k: v for k, v in data.items() if v is not None}}
        sub = await self.repo.create(merged)
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
        try:
            provider = registry.get(source)
        except KeyError:
            raise ValueError(f"Unknown source provider: {source}")
        url = data.get("source_url", "")
        if url:
            normalized = provider.normalize_url(url) or url
            if not provider.validate_url(normalized):
                raise ValueError(f"Invalid URL for source '{source}': {url}")
            data["source_url"] = normalized
        ss = await self.repo.add_source(data)
        await self.db.commit()

        # Auto-enrich creator with Danbooru reference data
        sub = await self.repo.get(ss.subscription_id)
        if sub:
            await self._enrich_creator_from_danbooru(sub.creator_id, url)

        return ss

    async def list_sources(self, subscription_id: UUID):
        return await self.repo.list_sources(subscription_id)

    async def update_source(self, ss_id: UUID, data: dict):
        ss = await self.repo.get_source(ss_id)
        if not ss:
            raise ValueError("SubscriptionSource not found")
        if "source_url" in data and data.get("source_url"):
            try:
                provider = registry.get(ss.source)
            except KeyError:
                raise ValueError(f"Unknown source provider: {ss.source}")
            normalized = provider.normalize_url(data["source_url"]) or data["source_url"]
            if not provider.validate_url(normalized):
                raise ValueError(f"Invalid URL for source '{ss.source}': {data['source_url']}")
            data["source_url"] = normalized
        ss = await self.repo.update_source(ss, data)
        await self.db.commit()
        return ss

    async def delete_source(self, ss_id: UUID):
        ss = await self.repo.get_source(ss_id)
        if not ss:
            raise ValueError("SubscriptionSource not found")
        await self.repo.delete_source(ss)
        await self.db.commit()

    async def _enrich_creator_from_danbooru(self, creator_id: UUID, source_url: str) -> int:
        """Query Danbooru for this source URL and create CreatorLink suggestions."""
        try:
            from app.services import danbooru as danbooru_svc
            from app.models import Creator

            artist, links = danbooru_svc.search_and_extract(source_url=source_url)
            if not links:
                return 0

            # Enrich creator record with Danbooru data
            c = await self.db.get(Creator, creator_id)
            if c and artist:
                if c.danbooru_artist_id is None:
                    c.danbooru_artist_id = artist.get("id")
                # Populate description with aliases if empty
                if not c.description:
                    parts = []
                    other_names = artist.get("other_names", [])
                    if other_names:
                        parts.append(f"Danbooru aliases: {', '.join(other_names)}")
                    notes = artist.get("notes")
                    if notes:
                        parts.append(notes)
                    if parts:
                        c.description = "\n".join(parts)
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

            if created or (c and c.danbooru_artist_id):
                await self.db.commit()
                logger.info("Danbooru enrichment: %d links created for creator %s from URL %s",
                            created, creator_id, source_url)
            return created
        except Exception as e:
            logger.warning("Danbooru enrichment failed for creator %s: %s", creator_id, e)
            return 0
