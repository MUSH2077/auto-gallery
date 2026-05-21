from uuid import UUID

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Subscription, SubscriptionSource, Creator


class SubscriptionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50,
                       search: str | None = None,
                       is_active: bool | None = None,
                       sync_enabled: bool | None = None,
                       never_synced: bool | None = None) -> list[Subscription]:
        conditions = []

        if search:
            conditions.append(
                Subscription.creator_id.in_(
                    select(Creator.id).where(or_(
                        Creator.name.ilike(f"%{search}%"),
                        Creator.display_name.ilike(f"%{search}%"),
                    ))
                )
            )
        if is_active is not None:
            conditions.append(Subscription.is_active == is_active)
        if sync_enabled is not None:
            conditions.append(Subscription.sync_enabled == sync_enabled)
        if never_synced is not None:
            if never_synced:
                conditions.append(Subscription.last_synced_at.is_(None))
            else:
                conditions.append(Subscription.last_synced_at.isnot(None))

        stmt = select(Subscription).offset(offset).limit(limit).order_by(Subscription.created_at.desc())
        if conditions:
            stmt = stmt.where(and_(*conditions))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, sub_id: UUID) -> Subscription | None:
        return await self.session.get(Subscription, sub_id)

    async def create(self, data: dict) -> Subscription:
        sub = Subscription(**data)
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def update(self, sub: Subscription, data: dict) -> Subscription:
        for key, value in data.items():
            if value is not None:
                setattr(sub, key, value)
        await self.session.flush()
        return sub

    async def delete(self, sub: Subscription) -> None:
        await self.session.delete(sub)
        await self.session.flush()

    async def add_source(self, data: dict) -> SubscriptionSource:
        ss = SubscriptionSource(**data)
        self.session.add(ss)
        await self.session.flush()
        return ss

    async def get_source(self, ss_id: UUID) -> SubscriptionSource | None:
        return await self.session.get(SubscriptionSource, ss_id)

    async def update_source(self, ss: SubscriptionSource, data: dict) -> SubscriptionSource:
        for key, value in data.items():
            if value is not None:
                setattr(ss, key, value)
        await self.session.flush()
        return ss

    async def list_sources(self, subscription_id: UUID) -> list[SubscriptionSource]:
        result = await self.session.execute(
            select(SubscriptionSource).where(SubscriptionSource.subscription_id == subscription_id)
        )
        return list(result.scalars().all())

    async def delete_source(self, ss: SubscriptionSource) -> None:
        await self.session.delete(ss)
        await self.session.flush()
