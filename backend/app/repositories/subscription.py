from uuid import UUID

from sqlalchemy import select

from app.models import Subscription, SubscriptionSource, Creator
from app.repositories.base import BaseRepository


class SubscriptionRepository(BaseRepository[Subscription]):
    model = Subscription

    async def update(self, obj: Subscription, data: dict) -> Subscription:
        """Allow explicit NULL for the two fields whose NULL means inherit."""

        nullable_fields = {"schedule_mode", "scheduled_times"}
        for key, value in data.items():
            if value is not None or key in nullable_fields:
                setattr(obj, key, value)
        await self.session.flush()
        return obj

    async def get(self, sub_id: UUID) -> Subscription | None:
        """Get a single subscription with creator name fields populated."""
        stmt = (
            select(Subscription, Creator.name, Creator.display_name)
            .join(Creator, Creator.id == Subscription.creator_id)
            .where(Subscription.id == sub_id)
        )
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            return None
        sub = row[0]
        _creator_name = (row[2] or "").strip()
        _creator_base = (row[1] or "").strip()
        sub.creator_display_name = _creator_name or None
        sub.creator_name = _creator_name or _creator_base or f"Creator {sub.creator_id}"
        return sub

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
