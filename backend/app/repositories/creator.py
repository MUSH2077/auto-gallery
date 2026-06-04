from uuid import UUID

from sqlalchemy import select, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Creator, SourceCreator, CreatorLink, Subscription, SubscriptionSource


class CreatorRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50,
                       search: str | None = None,
                       is_active: bool | None = None,
                       has_danbooru: bool | None = None,
                       has_subscription: bool | None = None,
                       is_favorite: bool | None = None) -> list[Creator]:
        conditions = []

        if search:
            conditions.append(or_(
                Creator.name.ilike(f"%{search}%"),
                Creator.display_name.ilike(f"%{search}%"),
            ))
        if is_active is not None:
            conditions.append(Creator.is_active == is_active)
        if has_danbooru is not None:
            if has_danbooru:
                conditions.append(Creator.danbooru_artist_id.isnot(None))
            else:
                conditions.append(Creator.danbooru_artist_id.is_(None))
        if has_subscription is not None:
            subq = select(Subscription.id).where(Subscription.creator_id == Creator.id)
            if has_subscription:
                conditions.append(subq.exists())
            else:
                conditions.append(~subq.exists())
        if is_favorite is not None:
            conditions.append(Creator.is_favorite == is_favorite)

        subscription_count = (
            select(func.count(Subscription.id))
            .where(Subscription.creator_id == Creator.id)
            .correlate(Creator)
            .scalar_subquery()
        )
        source_count = (
            select(func.count(SourceCreator.id))
            .where(SourceCreator.creator_id == Creator.id)
            .correlate(Creator)
            .scalar_subquery()
        )
        repository_count = (
            select(func.count(SubscriptionSource.id))
            .select_from(Subscription)
            .join(SubscriptionSource, SubscriptionSource.subscription_id == Subscription.id)
            .where(Subscription.creator_id == Creator.id)
            .where(SubscriptionSource.source_url.isnot(None))
            .correlate(Creator)
            .scalar_subquery()
        )
        last_synced_at = (
            select(func.max(SubscriptionSource.last_synced_at))
            .select_from(Subscription)
            .join(SubscriptionSource, SubscriptionSource.subscription_id == Subscription.id)
            .where(Subscription.creator_id == Creator.id)
            .correlate(Creator)
            .scalar_subquery()
        )

        stmt = (
            select(Creator, subscription_count, source_count, repository_count, last_synced_at)
            .offset(offset)
            .limit(limit)
            .order_by(Creator.name)
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))
        result = await self.session.execute(stmt)
        creators = []
        for row in result:
            creator = row[0]
            creator.subscription_count = row[1] or 0
            creator.source_count = row[2] or 0
            creator.repository_count = row[3] or 0
            creator.last_synced_at = row[4]
            creators.append(creator)
        return creators

    async def get(self, creator_id: UUID) -> Creator | None:
        return await self.session.get(Creator, creator_id)

    async def create(self, data: dict) -> Creator:
        creator = Creator(**data)
        self.session.add(creator)
        await self.session.flush()
        return creator

    async def update(self, creator: Creator, data: dict) -> Creator:
        for key, value in data.items():
            if value is not None:
                setattr(creator, key, value)
        await self.session.flush()
        return creator

    async def delete(self, creator: Creator) -> None:
        await self.session.delete(creator)
        await self.session.flush()

    async def add_source_creator(self, data: dict) -> SourceCreator:
        sc = SourceCreator(**data)
        self.session.add(sc)
        await self.session.flush()
        return sc

    async def get_source_creator(self, sc_id: UUID) -> SourceCreator | None:
        return await self.session.get(SourceCreator, sc_id)

    async def add_link(self, data: dict) -> CreatorLink:
        link = CreatorLink(**data)
        self.session.add(link)
        await self.session.flush()
        return link

    async def get_link(self, link_id: UUID) -> CreatorLink | None:
        return await self.session.get(CreatorLink, link_id)

    async def update_link(self, link: CreatorLink, data: dict) -> CreatorLink:
        for key, value in data.items():
            if value is not None:
                setattr(link, key, value)
        await self.session.flush()
        return link

    async def list_links(self, creator_id: UUID) -> list[CreatorLink]:
        result = await self.session.execute(
            select(CreatorLink)
            .where(CreatorLink.creator_id == creator_id)
            .order_by(CreatorLink.confidence.desc())
        )
        return list(result.scalars().all())

    async def list_source_creators(self, creator_id: UUID) -> list[SourceCreator]:
        from sqlalchemy import select
        result = await self.session.execute(
            select(SourceCreator).where(SourceCreator.creator_id == creator_id)
        )
        return list(result.scalars().all())

    async def delete_link(self, link: CreatorLink) -> None:
        await self.session.delete(link)
        await self.session.flush()
