from uuid import UUID

from sqlalchemy import select, func, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Work, WorkSource, Asset, AssetSource, SourceCreator


class WorkRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self, offset: int = 0, limit: int = 50,
                       search: str | None = None,
                       source: str | None = None,
                       creator_id: str | None = None,
                       is_nsfw: bool | None = None,
                       sort_by: str = "created_at",
                       sort_order: str = "desc") -> list[Work]:
        # Scalar subquery for source (first alphabetically)
        src_sub = (
            select(WorkSource.source)
            .where(WorkSource.work_id == Work.id)
            .correlate(Work)
            .limit(1)
            .scalar_subquery()
        )

        # Scalar subquery for creator name
        cname_sub = (
            select(SourceCreator.display_name)
            .select_from(WorkSource)
            .join(SourceCreator, SourceCreator.source_creator_id == WorkSource.source_creator_id)
            .where(WorkSource.work_id == Work.id)
            .correlate(Work)
            .limit(1)
            .scalar_subquery()
        )

        # Scalar subquery for creator UUID
        cid_sub = (
            select(SourceCreator.creator_id)
            .select_from(WorkSource)
            .join(SourceCreator, SourceCreator.source_creator_id == WorkSource.source_creator_id)
            .where(WorkSource.work_id == Work.id)
            .correlate(Work)
            .limit(1)
            .scalar_subquery()
        )

        stmt = (
            select(
                Work,
                func.coalesce(
                    select(func.count(AssetSource.id))
                    .select_from(WorkSource)
                    .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
                    .where(WorkSource.work_id == Work.id)
                    .correlate(Work)
                    .scalar_subquery(),
                    0,
                ).label("asset_count"),
                src_sub.label("source"),
                cname_sub.label("creator_name"),
                cid_sub.label("creator_id"),
            )
        )

        # Build WHERE conditions
        conditions = []
        if search:
            conditions.append(Work.title.ilike(f"%{search}%"))
        if is_nsfw is not None:
            conditions.append(Work.is_nsfw == is_nsfw)

        # For source and creator filters, need EXISTS subquery
        if source:
            conditions.append(
                Work.id.in_(
                    select(WorkSource.work_id).where(WorkSource.source == source)
                )
            )
        if creator_id:
            conditions.append(
                Work.id.in_(
                    select(WorkSource.work_id)
                    .join(SourceCreator, SourceCreator.source_creator_id == WorkSource.source_creator_id)
                    .where(SourceCreator.creator_id == UUID(creator_id))
                )
            )

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Sort
        sort_col = getattr(Work, sort_by, Work.created_at)
        stmt = stmt.order_by(sort_col.desc() if sort_order == "desc" else sort_col.asc())

        stmt = stmt.offset(offset).limit(limit)

        result = await self.session.execute(stmt)
        works = []
        for row in result:
            w = row[0]
            w.asset_count = row[1] or 0
            w.source = row[2]
            w.creator_name = row[3]
            w.creator_id = str(row[4]) if row[4] else None
            works.append(w)
        return works

    async def get(self, work_id: UUID) -> Work | None:
        return await self.session.get(Work, work_id)

    async def get_sources(self, work_id: UUID) -> list[WorkSource]:
        result = await self.session.execute(
            select(WorkSource).where(WorkSource.work_id == work_id)
        )
        return list(result.scalars().all())

    async def get_assets(self, work_source_id: UUID) -> list[Asset]:
        result = await self.session.execute(
            select(Asset)
            .join(AssetSource, AssetSource.asset_id == Asset.id)
            .where(AssetSource.work_source_id == work_source_id)
        )
        return list(result.scalars().all())
