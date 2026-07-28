from uuid import UUID

from sqlalchemy import select, or_, and_, func

from app.models import Creator, SourceCreator, CreatorLink, Subscription, SubscriptionSource
from app.repositories.base import BaseRepository


class CreatorRepository(BaseRepository[Creator]):
    model = Creator

    async def list_all(self, offset: int = 0, limit: int = 50,
                       search: str | None = None,
                       search_ids: list[UUID] | None = None,
                       is_active: bool | None = None,
                       has_danbooru: bool | None = None,
                       has_subscription: bool | None = None,
                       is_favorite: bool | None = None) -> tuple[list[Creator], int]:
        # ── Filter conditions ──────────────────────────────────────────
        conditions = []

        if search and search_ids:
            conditions.append(or_(
                Creator.id.in_(search_ids),
                Creator.name.ilike(f"%{search}%"),
                Creator.display_name.ilike(f"%{search}%"),
            ))
        elif search:
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

        # ── Pre-aggregated subqueries (derived tables, not per-row) ────
        # Subscription aggregates: count, repository count, last synced
        sub_agg = (
            select(
                Subscription.creator_id,
                func.count(func.distinct(Subscription.id)).label("sub_count"),
                func.count(func.distinct(SubscriptionSource.id))
                    .filter(SubscriptionSource.source_url.isnot(None))
                    .label("repo_count"),
                func.max(SubscriptionSource.last_synced_at).label("last_sync"),
            )
            .outerjoin(SubscriptionSource,
                       SubscriptionSource.subscription_id == Subscription.id)
            .group_by(Subscription.creator_id)
            .subquery()
        )

        # Source creator count
        sc_agg = (
            select(
                SourceCreator.creator_id,
                func.count(SourceCreator.id).label("src_count"),
            )
            .where(SourceCreator.creator_id.isnot(None))
            .group_by(SourceCreator.creator_id)
            .subquery()
        )

        # ── Main query with JOINs ───────────────────────────────────────
        stmt = (
            select(
                Creator,
                func.coalesce(sub_agg.c.sub_count, 0).label("subscription_count"),
                func.coalesce(sc_agg.c.src_count, 0).label("source_count"),
                func.coalesce(sub_agg.c.repo_count, 0).label("repository_count"),
                sub_agg.c.last_sync.label("last_synced_at"),
            )
            .outerjoin(sub_agg, sub_agg.c.creator_id == Creator.id)
            .outerjoin(sc_agg, sc_agg.c.creator_id == Creator.id)
            .order_by(Creator.created_at.desc())
        )

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # ── Count query (same filters) ──────────────────────────────────
        count_stmt = select(func.count(Creator.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total = (await self.session.execute(count_stmt)).scalar_one()

        # ── Paginate ────────────────────────────────────────────────────
        stmt = stmt.offset(offset).limit(limit)

        result = await self.session.execute(stmt)
        creators = []
        for row in result:
            creator = row[0]
            creator.subscription_count = row[1] or 0
            creator.source_count = row[2] or 0
            creator.repository_count = row[3] or 0
            creator.last_synced_at = row[4]
            creators.append(creator)

        return creators, total

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
        result = await self.session.execute(
            select(SourceCreator).where(SourceCreator.creator_id == creator_id)
        )
        return list(result.scalars().all())

    async def delete_link(self, link: CreatorLink) -> None:
        await self.session.delete(link)
        await self.session.flush()
