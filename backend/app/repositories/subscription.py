from uuid import UUID

from sqlalchemy import select, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Subscription, SubscriptionSource, Creator, DownloadJob


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

        source_count = (
            select(func.count(SubscriptionSource.id))
            .where(SubscriptionSource.subscription_id == Subscription.id)
            .correlate(Subscription)
            .scalar_subquery()
        )
        enabled_source_count = (
            select(func.count(SubscriptionSource.id))
            .where(SubscriptionSource.subscription_id == Subscription.id)
            .where(SubscriptionSource.is_enabled == True)
            .correlate(Subscription)
            .scalar_subquery()
        )
        latest_job_status = (
            select(DownloadJob.status)
            .where(DownloadJob.subscription_id == Subscription.id)
            .order_by(DownloadJob.created_at.desc())
            .limit(1)
            .correlate(Subscription)
            .scalar_subquery()
        )

        stmt = (
            select(Subscription, Creator.name, Creator.display_name, source_count, enabled_source_count, latest_job_status)
            .join(Creator, Creator.id == Subscription.creator_id)
            .offset(offset).limit(limit)
            .order_by(Subscription.created_at.desc())
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))
        result = await self.session.execute(stmt)
        subs = []
        for row in result:
            sub = row[0]
            # Prefer display_name, fall back to name.  Never expose raw UUID
            # as a human-readable name — if both are missing the Creator record
            # is malformed, but we still show a sane label.
            _creator_name = (row[2] or "").strip()
            _creator_base = (row[1] or "").strip()
            sub.creator_display_name = _creator_name or None
            sub.creator_name = _creator_name or _creator_base or f"Creator {sub.creator_id}"
            sub.source_count = row[3] or 0
            sub.enabled_source_count = row[4] or 0
            sub.latest_job_status = row[5]
            subs.append(sub)
        return subs

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
